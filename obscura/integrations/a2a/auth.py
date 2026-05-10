"""obscura.integrations.a2a.auth — A2A-specific bearer token auth middleware.

Validates ``Authorization: Bearer <token>`` on all ``/a2a/*`` paths.
The ``/.well-known/agent.json`` discovery endpoint is always public.

Token sources (first one that yields tokens wins):

1. ``OBSCURA_A2A_TOKEN`` env var — colon-separated for multiple tokens.
2. ``~/.obscura/a2a-gateway.token`` file (one token per line, ``#`` comments
   stripped, blank lines ignored).

Usage in ``create_standalone_app``::

    app.add_middleware(A2ABearerAuthMiddleware)
    # or with an explicit token file:
    app.add_middleware(
        A2ABearerAuthMiddleware,
        token_file=Path("~/.obscura/a2a-gateway.token").expanduser(),
    )
"""

from __future__ import annotations

import logging
import os
import threading
import time as _time
from pathlib import Path
from typing import TYPE_CHECKING, Any, override

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse, Response

if TYPE_CHECKING:
    from starlette.requests import Request

logger = logging.getLogger(__name__)

# Public paths that require NO auth (A2A discovery endpoint).
_PUBLIC_PATHS: tuple[str, ...] = ("/.well-known/",)

# Default token-file location.
_DEFAULT_TOKEN_FILE = Path.home() / ".obscura" / "a2a-gateway.token"


def _load_tokens_from_file(path: Path) -> list[str]:
    """Read bearer tokens from *path* (one per line, ``#`` comments stripped).

    Returns an empty list if the file does not exist or is unreadable.
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []

    tokens: list[str] = []
    for raw in lines:
        # Strip inline comments and surrounding whitespace.
        line = raw.split("#", 1)[0].strip()
        if line:
            tokens.append(line)
    return tokens


def _resolve_tokens(token_file: Path | None) -> frozenset[str]:
    """Resolve the set of accepted bearer tokens at middleware startup.

    Priority:
    1. ``OBSCURA_A2A_TOKEN`` env var (colon-separated).
    2. *token_file* if provided.
    3. ``~/.obscura/a2a-gateway.token`` (default location).
    """
    env_val = os.environ.get("OBSCURA_A2A_TOKEN", "").strip()
    if env_val:
        tokens = [t.strip() for t in env_val.split(":") if t.strip()]
        if tokens:
            logger.debug("A2A auth: loaded %d token(s) from OBSCURA_A2A_TOKEN", len(tokens))
            return frozenset(tokens)

    path = token_file if token_file is not None else _DEFAULT_TOKEN_FILE
    tokens = _load_tokens_from_file(path)
    if tokens:
        logger.debug("A2A auth: loaded %d token(s) from %s", len(tokens), path)
    else:
        logger.warning(
            "A2A auth: no tokens found (env OBSCURA_A2A_TOKEN unset, %s missing/empty). "
            "All /a2a/* requests will be rejected.",
            path,
        )
    return frozenset(tokens)


class A2ABearerAuthMiddleware(BaseHTTPMiddleware):
    """Require ``Authorization: Bearer <token>`` on all ``/a2a/*`` paths.

    The ``/.well-known/`` prefix is always public (A2A discovery endpoint).
    Any other ``/a2a/*`` request without a matching token receives a
    ``401 {"error": "unauthorized"}`` response.

    Rejected attempts are logged at WARNING level with the client IP and
    path — but never with the token value itself.
    """

    def __init__(
        self,
        app: Any,
        *,
        token_file: Path | None = None,
    ) -> None:
        super().__init__(app)
        self._tokens: frozenset[str] = _resolve_tokens(token_file)
        self._lock = threading.Lock()

    @classmethod
    def from_token_file(cls, path: Path, app: Any) -> "A2ABearerAuthMiddleware":
        """Construct middleware that loads tokens exclusively from *path*."""
        return cls(app, token_file=path)

    @override
    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        path = request.url.path

        # Always allow public discovery paths.
        if any(path.startswith(prefix) for prefix in _PUBLIC_PATHS):
            return await call_next(request)

        # Only intercept /a2a/* paths.
        if not path.startswith("/a2a/") and path != "/a2a":
            return await call_next(request)

        # If no tokens are configured, reject everything — fail closed.
        if not self._tokens:
            client_ip = _client_ip(request)
            logger.warning(
                "A2A auth rejected (no tokens configured): ip=%s path=%s",
                client_ip,
                path,
            )
            return JSONResponse(
                status_code=401,
                content={"error": "unauthorized"},
            )

        token = _extract_bearer(request)
        if token and token in self._tokens:
            return await call_next(request)

        client_ip = _client_ip(request)
        logger.warning(
            "A2A auth rejected: ip=%s path=%s reason=%s",
            client_ip,
            path,
            "missing_token" if not token else "invalid_token",
        )
        return JSONResponse(
            status_code=401,
            content={"error": "unauthorized"},
        )


def _extract_bearer(request: Request) -> str | None:
    """Return the raw token from ``Authorization: Bearer <token>``, or ``None``."""
    header = request.headers.get("Authorization") or request.headers.get("authorization")
    if not header:
        return None
    lower = header.lower()
    if not lower.startswith("bearer "):
        return None
    token = header[7:].strip()
    return token if token else None


def _client_ip(request: Request) -> str:
    """Best-effort client IP extraction."""
    if os.environ.get("OBSCURA_TRUST_PROXY_HEADERS", "").strip().lower() in ("1", "true"):
        xff = request.headers.get("X-Forwarded-For")
        if xff:
            return xff.split(",")[0].strip()
    client = getattr(request, "client", None)
    return client.host if client else ""


# ---------------------------------------------------------------------------
# Per-IP sliding-window rate limiter for A2A endpoints
# ---------------------------------------------------------------------------

_RATE_LIMIT_WINDOW_SECONDS = 60


def _default_a2a_rate_limit() -> int:
    """Return the configured A2A inbound rate limit (req/min per IP)."""
    from obscura.core.config import ObscuraConfig

    return ObscuraConfig.load().a2a_inbound_rate_limit


class A2ARateLimitMiddleware(BaseHTTPMiddleware):
    """Sliding-window per-IP rate limiter for ``/a2a/*`` paths.

    Allows up to ``max_requests`` requests per 60-second window per client IP.
    Defaults to ``OscuraConfig.load().a2a_inbound_rate_limit`` (60) if not
    specified.  Override via ``OBSCURA_A2A_INBOUND_RATE_LIMIT`` env var or
    the ``runtime.a2a_inbound_rate_limit`` key in ``~/.obscura/settings.json``.

    Returns ``429 {"error": "rate_limit_exceeded"}`` with a
    ``Retry-After`` header when the limit is breached.

    ``/.well-known/`` paths are exempt (same as bearer auth).
    """

    def __init__(
        self,
        app: Any,
        *,
        max_requests: int | None = None,
        window_seconds: int = _RATE_LIMIT_WINDOW_SECONDS,
    ) -> None:
        super().__init__(app)
        self._max = max_requests if max_requests is not None else _default_a2a_rate_limit()
        self._window = window_seconds
        # { ip: [timestamp, ...] }
        self._buckets: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    @override
    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        path = request.url.path

        # Public discovery paths are exempt.
        if any(path.startswith(prefix) for prefix in _PUBLIC_PATHS):
            return await call_next(request)

        # Only rate-limit /a2a/* paths.
        if not path.startswith("/a2a/") and path != "/a2a":
            return await call_next(request)

        ip = _client_ip(request)
        if not ip:
            # No IP — pass through (shouldn't happen in practice).
            return await call_next(request)

        now = _time.monotonic()
        cutoff = now - self._window

        with self._lock:
            bucket = self._buckets.setdefault(ip, [])
            # Trim stale timestamps.
            fresh = [t for t in bucket if t > cutoff]
            if len(fresh) >= self._max:
                self._buckets[ip] = fresh
                retry_after = int(self._window - (now - fresh[0]) + 1)
                logger.warning(
                    "A2A rate limit exceeded: ip=%s path=%s",
                    ip,
                    path,
                )
                return JSONResponse(
                    status_code=429,
                    content={"error": "rate_limit_exceeded"},
                    headers={"Retry-After": str(max(retry_after, 1))},
                )
            fresh.append(now)
            self._buckets[ip] = fresh

        return await call_next(request)


__all__ = ["A2ABearerAuthMiddleware", "A2ARateLimitMiddleware"]
