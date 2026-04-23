"""obscura.auth.middleware -- Authentication middleware.

Validates incoming credentials on ``/api/*`` routes and populates
``request.state.user`` with an :class:`~obscura.auth.models.AuthenticatedUser`.

Credential order:

1. ``X-API-Key`` header — the local / machine-to-machine bypass
   (:func:`obscura.auth.rbac.user_from_api_key`).
2. ``Authorization: Bearer <jwt>`` — Supabase OAuth, validated via
   :mod:`obscura.auth.supabase`.

Non-``/api/`` paths (``/health``, ``/ready``, etc.) pass through without
authentication.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Any, override

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse, Response

from obscura.auth.rbac import user_from_api_key
from obscura.auth.supabase import SupabaseAuthError, get_verifier

# Per-IP exponentially-decaying auth-failure counter. Not a perfect DDoS
# defence — the real fix is a WAF in front of the server — but it stops
# trivial credential-stuffing loops from getting free infinite tries.
_AUTH_FAILURE_WINDOW_SECONDS = 60
_AUTH_FAILURE_THRESHOLD = 20  # per-IP per minute before 429

if TYPE_CHECKING:
    from starlette.requests import Request

    from obscura.auth.models import AuthenticatedUser

logger = logging.getLogger(__name__)


class APIKeyAuthMiddleware(BaseHTTPMiddleware):
    """Accepts ``X-API-Key`` or Supabase ``Authorization: Bearer`` on ``/api/*``."""

    def __init__(self, app: Any) -> None:
        super().__init__(app)
        # Track per-IP failure timestamps; trimmed on each check. Process-
        # local (sufficient for a single-instance deployment; for multi-
        # instance use a Redis-backed counter).
        self._failures: dict[str, list[float]] = {}
        self._failures_lock = threading.Lock()

    @override
    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        if not _is_protected_path(request.url.path):
            return await call_next(request)

        client_ip = _client_ip(request)

        # Pre-check: if this IP has been hammering us with bad credentials,
        # 429 before we even bother validating.
        if self._is_throttled(client_ip):
            _emit_auth_audit(
                request.url.path,
                "anonymous",
                "",
                "rate_limited",
                reason="auth_failure_throttled",
                client_ip=client_ip,
            )
            return JSONResponse(
                status_code=429,
                content={"detail": "Too many auth failures. Slow down."},
            )

        # 1. Local / machine bypass
        api_key = request.headers.get("X-API-Key")
        user = user_from_api_key(api_key)
        if user is not None:
            request.state.user = user
            return await call_next(request)

        # 2. Supabase bearer token
        user = _user_from_bearer(request)
        if user is not None:
            request.state.user = user
            # Soft session binding — log but don't reject on UA/IP drift.
            if user.session_id:
                try:
                    from obscura.auth.session_binding import observe_binding

                    ua = request.headers.get("User-Agent", "")
                    observe_binding(user.session_id, ua, client_ip)
                except Exception:
                    logger.exception("Session binding observation failed")
            return await call_next(request)

        self._record_failure(client_ip)
        _emit_auth_audit(
            request.url.path,
            "anonymous",
            "",
            "denied",
            reason="missing_credentials",
            client_ip=client_ip,
        )
        return JSONResponse(
            status_code=401,
            content={"detail": "Missing or invalid credentials"},
        )

    def _record_failure(self, ip: str) -> None:
        if not ip:
            return
        import time as _time

        now = _time.time()
        cutoff = now - _AUTH_FAILURE_WINDOW_SECONDS
        with self._failures_lock:
            bucket = self._failures.setdefault(ip, [])
            bucket.append(now)
            # Drop timestamps that fell out of the window.
            self._failures[ip] = [t for t in bucket if t > cutoff]

    def _is_throttled(self, ip: str) -> bool:
        if not ip:
            return False
        import time as _time

        cutoff = _time.time() - _AUTH_FAILURE_WINDOW_SECONDS
        with self._failures_lock:
            bucket = self._failures.get(ip, [])
            fresh = [t for t in bucket if t > cutoff]
            if fresh:
                self._failures[ip] = fresh
            return len(fresh) >= _AUTH_FAILURE_THRESHOLD


# Paths we always guard. Anything under these prefixes needs valid
# credentials. Discovery / liveness endpoints are excluded explicitly
# below so unauth'd clients can at least find the service.
_PROTECTED_PREFIXES = ("/api/", "/mcp/", "/a2a/")
# Publicly-accessible endpoints that MUST stay open (liveness probes,
# agent discovery manifests).
_PUBLIC_PATHS = (
    "/health",
    "/ready",
    "/.well-known/",  # A2A + ACME + etc.
)


def _is_protected_path(path: str) -> bool:
    if any(path.startswith(p) for p in _PUBLIC_PATHS):
        return False
    return any(path.startswith(p) for p in _PROTECTED_PREFIXES)


def _client_ip(request: Request) -> str:
    """Best-effort client IP. Trust ``X-Forwarded-For`` only when a proxy is
    in front — configure via ``OBSCURA_TRUST_PROXY_HEADERS=true`` to opt in.
    """
    import os as _os

    if _os.environ.get("OBSCURA_TRUST_PROXY_HEADERS", "").strip().lower() in (
        "1",
        "true",
    ):
        xff = request.headers.get("X-Forwarded-For")
        if xff:
            return xff.split(",")[0].strip()
    client = getattr(request, "client", None)
    return client.host if client else ""


def _user_from_bearer(request: Request) -> AuthenticatedUser | None:
    """Validate an ``Authorization: Bearer <jwt>`` against Supabase."""
    auth_header = request.headers.get("Authorization") or request.headers.get(
        "authorization",
    )
    if not auth_header or not auth_header.lower().startswith("bearer "):
        return None

    token = auth_header.split(" ", 1)[1].strip()
    if not token:
        return None

    config = getattr(request.app.state, "config", None)
    if config is None:
        return None

    jwt_secret = getattr(config, "supabase_jwt_secret", "") or ""
    jwks_url = getattr(config, "supabase_jwks_url", "") or ""
    if not jwt_secret and not jwks_url:
        return None

    audience = getattr(config, "supabase_audience", "authenticated") or "authenticated"
    issuer = getattr(config, "supabase_issuer", "") or ""
    if not issuer and getattr(config, "supabase_url", ""):
        issuer = f"{config.supabase_url.rstrip('/')}/auth/v1"

    verifier = get_verifier(jwt_secret, jwks_url, audience, issuer)
    try:
        return verifier.verify(token)
    except SupabaseAuthError as exc:
        logger.debug("Supabase token rejected: %s", exc)
        _emit_auth_audit(
            request.url.path,
            "anonymous",
            "",
            "denied",
            reason=f"bad_supabase_token: {exc}",
        )
        return None


# ---------------------------------------------------------------------------
# Auth audit helper
# ---------------------------------------------------------------------------


_DROPPED_AUDIT_EVENTS = 0


def _emit_auth_audit(
    path: str,
    user_id: str,
    email: str,
    outcome: str,
    **details: Any,
) -> None:
    """Emit an audit event for auth decisions.

    Failures are logged (not silenced) so a broken audit backend is visible
    in logs and ``_DROPPED_AUDIT_EVENTS`` reflects the cumulative count —
    critical for forensic trails and security incident response.
    """
    global _DROPPED_AUDIT_EVENTS
    try:
        from obscura.telemetry.audit import AuditEvent, emit_audit_event

        emit_audit_event(
            AuditEvent(
                event_type="auth.validate",
                user_id=user_id,
                user_email=email,
                resource=f"endpoint:{path}",
                action="authenticate",
                outcome=outcome,
                details=details,
            ),
        )
    except Exception:
        _DROPPED_AUDIT_EVENTS += 1
        logger.exception(
            "Auth audit event dropped (total dropped: %d) path=%s outcome=%s",
            _DROPPED_AUDIT_EVENTS,
            path,
            outcome,
        )


def get_dropped_audit_count() -> int:
    """Observability helper — expose the dropped-audit counter."""
    return _DROPPED_AUDIT_EVENTS
