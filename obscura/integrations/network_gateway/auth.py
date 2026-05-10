"""obscura.integrations.network_gateway.auth — Bearer auth + rate limiting.

Two middlewares:

* :class:`GatewayBearerAuthMiddleware` — requires ``Authorization: Bearer
  <token>`` on all protected paths.  Skips ``/health`` and
  ``/.well-known/``.  When no token is configured auth is bypassed (warn
  logged once).
* :class:`GatewayRateLimitMiddleware` — sliding-window per-IP rate limiter.
  Exempt paths: ``/health``, ``/.well-known/``.
"""

from __future__ import annotations

import logging
import threading
import time as _time
from typing import Any, override

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse, Response

logger = logging.getLogger(__name__)

# Paths that never require auth or rate limiting
_PUBLIC_PREFIXES: tuple[str, ...] = ("/health", "/.well-known/")

_RATE_WINDOW_SECONDS = 60


def _client_ip(request: Any) -> str:
    """Best-effort client IP — trust X-Forwarded-For only on loopback."""
    client = getattr(request, "client", None)
    host: str = client.host if client else ""
    if host in ("127.0.0.1", "::1"):
        xff = request.headers.get("X-Forwarded-For")
        if xff:
            return xff.split(",")[0].strip()
    return host


def _extract_bearer(request: Any) -> str | None:
    header: str = (
        request.headers.get("Authorization")
        or request.headers.get("authorization")
        or ""
    )
    if not header.lower().startswith("bearer "):
        return None
    token = header[7:].strip()
    return token or None


class GatewayBearerAuthMiddleware(BaseHTTPMiddleware):
    """Require ``Authorization: Bearer <token>`` on all non-public paths.

    The token is taken from ``GatewayConfig.token`` at app build time. If
    no token is configured, the middleware logs a warning and passes every
    request (permissive-no-auth mode, useful for fully-private deployments).
    """

    def __init__(self, app: Any, *, token: str) -> None:
        super().__init__(app)
        self._token: str = token
        if not token:
            logger.warning(
                "Network gateway: no bearer token configured — all requests will be accepted. "
                "Set OBSCURA_NETWORK_TOKEN or create ~/.obscura/network-gateway.token to enable auth."
            )

    @override
    async def dispatch(
        self,
        request: Any,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        path: str = request.url.path

        if any(path.startswith(p) for p in _PUBLIC_PREFIXES) or path == "/health":
            return await call_next(request)

        # No token configured → open access
        if not self._token:
            return await call_next(request)

        token = _extract_bearer(request)
        if token and token == self._token:
            return await call_next(request)

        logger.warning(
            "Gateway auth rejected: ip=%s path=%s reason=%s",
            _client_ip(request),
            path,
            "missing_token" if not token else "invalid_token",
        )
        return JSONResponse(
            status_code=401,
            content={"error": "unauthorized"},
            headers={"WWW-Authenticate": "Bearer"},
        )


class GatewayRateLimitMiddleware(BaseHTTPMiddleware):
    """Sliding-window per-IP rate limiter (60 req / min by default).

    Returns ``429 {"error": "rate_limit_exceeded"}`` with a ``Retry-After``
    header when the limit is breached. Public paths are exempt.
    """

    def __init__(self, app: Any, *, max_requests: int = 60) -> None:
        super().__init__(app)
        self._max = max_requests
        self._buckets: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    @override
    async def dispatch(
        self,
        request: Any,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        path: str = request.url.path

        if any(path.startswith(p) for p in _PUBLIC_PREFIXES) or path == "/health":
            return await call_next(request)

        ip = _client_ip(request)
        if not ip:
            return await call_next(request)

        now = _time.monotonic()
        cutoff = now - _RATE_WINDOW_SECONDS

        with self._lock:
            bucket = self._buckets.setdefault(ip, [])
            fresh = [t for t in bucket if t > cutoff]
            if len(fresh) >= self._max:
                self._buckets[ip] = fresh
                retry_after = int(_RATE_WINDOW_SECONDS - (now - fresh[0]) + 1)
                logger.warning("Gateway rate limit exceeded: ip=%s path=%s", ip, path)
                return JSONResponse(
                    status_code=429,
                    content={"error": "rate_limit_exceeded"},
                    headers={"Retry-After": str(max(retry_after, 1))},
                )
            fresh.append(now)
            self._buckets[ip] = fresh

        return await call_next(request)


__all__ = ["GatewayBearerAuthMiddleware", "GatewayRateLimitMiddleware"]
