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

from obscura.memory import MemoryStore
from obscura.vector_memory.vector_memory import VectorMemoryStore

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

# Process-local cache of users we've already provisioned to avoid repeating
# first-login initialization work on every request.
_PROVISIONED_USERS: set[str] = set()
_PROVISIONED_USERS_LOCK = threading.Lock()


def _ensure_user_account(user: AuthenticatedUser) -> None:
    """Provision local per-user state on first successful Supabase auth."""
    with _PROVISIONED_USERS_LOCK:
        if user.user_id in _PROVISIONED_USERS:
            return

    MemoryStore.for_user(user)
    VectorMemoryStore.for_user(user)

    with _PROVISIONED_USERS_LOCK:
        _PROVISIONED_USERS.add(user.user_id)


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
            # Revocation + idle-timeout checks. Bearer-tokened sessions
            # (when the Supabase path merges) will carry a JTI;
            # API-key flows reuse the key-derived session id so admin
            # revocation can reach both.
            jti = _extract_jti(request, user)
            session_id = _extract_session_id(request, user)
            if jti and _is_token_revoked(jti):
                _emit_auth_audit(
                    request.url.path,
                    user.user_id,
                    user.email,
                    "denied",
                    reason="token_revoked",
                    jti=jti,
                )
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Token has been revoked"},
                )
            if session_id and _is_session_idle(session_id):
                _emit_auth_audit(
                    request.url.path,
                    user.user_id,
                    user.email,
                    "denied",
                    reason="session_idle_timeout",
                    session_id=session_id,
                )
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Session idle timeout — re-authenticate"},
                )
            if session_id:
                _observe_session_activity(session_id)

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
        user = verifier.verify(token)
        _ensure_user_account(user)
        return user
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
# Revocation + idle-timeout helpers
# ---------------------------------------------------------------------------


def _extract_jti(request: Request, user: Any) -> str:
    """Return the JTI for this request's token, if known.

    Bearer-tokened requests (when the Supabase JWT path lands on this
    branch) carry a JTI in the validated claims — middleware stashes it
    on ``request.state.token_jti``. API-key flows have no JTI of their
    own, but an operator can still revoke an API key by putting the key
    itself in the blocklist under its own identifier; callers set
    ``request.state.token_jti`` to that identifier if they want this
    middleware to enforce it.
    """
    jti = getattr(request.state, "token_jti", None)
    if isinstance(jti, str) and jti:
        return jti
    return ""


def _extract_session_id(request: Request, user: Any) -> str:
    """Return a stable session identifier for idle-timeout tracking."""
    session_id = getattr(request.state, "session_id", None)
    if isinstance(session_id, str) and session_id:
        return session_id
    # API-key flow: there's one implicit session per (user_id, api_key).
    # user_id alone is usually enough — one idle window per user.
    return getattr(user, "user_id", "") or ""


def _is_token_revoked(jti: str) -> bool:
    """Cheap wrapper around the default blocklist — isolated so tests
    can monkeypatch without importing internals."""
    try:
        from obscura.auth.revocation import default_blocklist

        return default_blocklist().is_revoked(jti)
    except Exception:  # noqa: BLE001 — never fail open on our own bug
        return False


def _is_session_idle(session_id: str) -> bool:
    try:
        from obscura.auth.session_activity import default_tracker

        return default_tracker().is_idle(session_id)
    except Exception:  # noqa: BLE001
        return False


def _observe_session_activity(session_id: str) -> None:
    try:
        from obscura.auth.session_activity import default_tracker

        default_tracker().observe(session_id)
    except Exception:  # noqa: BLE001
        pass


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
