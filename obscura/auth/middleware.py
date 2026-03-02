"""
obscura.auth.middleware -- API key authentication middleware.

Validates API keys on ``/api/*`` routes and populates ``request.state.user``
with an :class:`~obscura.auth.models.AuthenticatedUser`.

API keys are loaded from the ``OBSCURA_API_KEYS`` environment variable
(parsed by :func:`obscura.auth.rbac.user_from_api_key`).
"""

from __future__ import annotations

import logging
from typing import Any, override

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from obscura.auth.rbac import user_from_api_key

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
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        # Skip auth for non-API routes
        if not request.url.path.startswith("/api/"):
            return await call_next(request)

        api_key = request.headers.get("X-API-Key")
        user = user_from_api_key(api_key)

        if user is not None:
            request.state.user = user
            return await call_next(request)

        # No valid API key — reject
        _emit_auth_audit(
            request.url.path, "anonymous", "", "denied", reason="missing_api_key"
        )
        return JSONResponse(
            status_code=401,
            content={"detail": "Missing or invalid API key"},
        )


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
            )
        )
    except Exception:
        pass
