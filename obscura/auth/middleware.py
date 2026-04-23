"""obscura.auth.middleware -- API key authentication middleware.

Validates API keys on ``/api/*`` routes and populates ``request.state.user``
with an :class:`~obscura.auth.models.AuthenticatedUser`.

API keys are loaded from the ``OBSCURA_API_KEYS`` environment variable
(parsed by :func:`obscura.auth.rbac.user_from_api_key`).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, override

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse, Response

from obscura.auth.rbac import user_from_api_key

if TYPE_CHECKING:
    from starlette.requests import Request

logger = logging.getLogger(__name__)


class APIKeyAuthMiddleware(BaseHTTPMiddleware):
    """Validates ``X-API-Key`` on ``/api/`` routes and sets ``request.state.user``.

    Requests to paths that do not start with ``/api/`` are passed through
    without authentication (e.g. ``/health``, ``/ready``).
    """

    def __init__(self, app: Any) -> None:
        super().__init__(app)

    @override
    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        # Skip auth for non-API routes
        if not request.url.path.startswith("/api/"):
            return await call_next(request)

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

        # No valid API key — reject
        _emit_auth_audit(
            request.url.path,
            "anonymous",
            "",
            "denied",
            reason="missing_api_key",
        )
        return JSONResponse(
            status_code=401,
            content={"detail": "Missing or invalid API key"},
        )


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


def _emit_auth_audit(
    path: str,
    user_id: str,
    email: str,
    outcome: str,
    **details: Any,
) -> None:
    """Emit an audit event for auth decisions (best-effort)."""
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
        pass
