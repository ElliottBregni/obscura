"""obscura.auth.middleware -- Authentication middleware.

Validates incoming credentials on ``/api/*`` routes and populates
``request.state.user`` with an :class:`~obscura.auth.models.AuthenticatedUser`.

Two credential types are accepted, in this order:

1. ``X-API-Key`` header — loaded from ``OBSCURA_API_KEYS``
   (see :func:`obscura.auth.rbac.user_from_api_key`). This is the local /
   machine-to-machine bypass and always wins when present.
2. ``Authorization: Bearer <jwt>`` header — verified against Supabase using
   :mod:`obscura.auth.supabase`. This is the primary human identity path.

Non-``/api/`` paths (``/health``, ``/ready``, ``/mcp``, etc.) pass through
without authentication.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, override

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse, Response

from obscura.auth.rbac import user_from_api_key
from obscura.auth.supabase import SupabaseAuthError, get_verifier

if TYPE_CHECKING:
    from starlette.requests import Request

    from obscura.auth.models import AuthenticatedUser

logger = logging.getLogger(__name__)


class APIKeyAuthMiddleware(BaseHTTPMiddleware):
    """Validates ``X-API-Key`` or Supabase ``Authorization: Bearer`` headers.

    The name is historical — the middleware also accepts Supabase-minted JWTs
    as a primary identity provider. Requests to paths that do not start with
    ``/api/`` are passed through without authentication (e.g. ``/health``,
    ``/ready``).
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

        # 1. Local / machine bypass: X-API-Key
        api_key = request.headers.get("X-API-Key")
        user = user_from_api_key(api_key)
        if user is not None:
            request.state.user = user
            return await call_next(request)

        # 2. Supabase bearer token
        user = _user_from_bearer(request)
        if user is not None:
            request.state.user = user
            return await call_next(request)

        # No valid credential — reject
        _emit_auth_audit(
            request.url.path,
            "anonymous",
            "",
            "denied",
            reason="missing_credentials",
        )
        return JSONResponse(
            status_code=401,
            content={"detail": "Missing or invalid credentials"},
        )


def _user_from_bearer(request: Request) -> AuthenticatedUser | None:
    """Validate an ``Authorization: Bearer <jwt>`` header against Supabase.

    Returns ``None`` when Supabase is not configured, the header is missing,
    or the token is invalid.
    """
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
        # Supabase not configured — treat bearer token as absent.
        return None

    audience = getattr(config, "supabase_audience", "authenticated") or "authenticated"
    issuer = getattr(config, "supabase_issuer", "") or ""
    if not issuer and getattr(config, "supabase_url", ""):
        issuer = f"{config.supabase_url.rstrip('/')}/auth/v1"

    verifier = get_verifier(jwt_secret, jwks_url, audience, issuer)
    try:
        user = verifier.verify(token)
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

    return user


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
