"""
sdk.auth.rbac -- Role-based access control dependencies for FastAPI.

Provides ``Depends()``-compatible callables for extracting the current
user and enforcing role requirements on individual endpoints.

Roles
-----
- ``admin``           -- full access, bypasses all role checks
- ``agent:copilot``   -- may invoke the Copilot backend
- ``agent:claude``    -- may invoke the Claude backend
- ``agent:read``      -- read-only agent access (send / stream)
- ``sync:write``      -- trigger vault sync
- ``sessions:manage`` -- create / delete sessions
"""

from __future__ import annotations

from typing import Callable

from fastapi import Depends, HTTPException, Request

from sdk.auth.models import AuthenticatedUser

# Mock user for when auth is disabled
_MOCK_USER = AuthenticatedUser(
    user_id="anonymous",
    email="anonymous@obscura.local",
    roles=("admin", "agent:copilot", "agent:claude", "agent:read", "sync:write", "sessions:manage"),
    org_id="local",
    token_type="anonymous",
    raw_token="",
)


# ---------------------------------------------------------------------------
# Core dependency: extract user from request
# ---------------------------------------------------------------------------

async def get_current_user(request: Request) -> AuthenticatedUser:
    """FastAPI dependency that returns the authenticated user.

    The :class:`~sdk.auth.middleware.JWTAuthMiddleware` must run before
    this dependency is evaluated -- it populates ``request.state.user``.

    If auth is disabled, returns a mock user with all roles.

    Raises:
        HTTPException(401): if no user is attached to the request.
    """
    # Check if auth is disabled via app config
    config = getattr(request.app.state, "config", None)
    if config is not None and not getattr(config, "auth_enabled", True):
        return _MOCK_USER
    
    user: AuthenticatedUser | None = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


# ---------------------------------------------------------------------------
# Role enforcement
# ---------------------------------------------------------------------------

def require_role(role: str) -> Callable:
    """Return a FastAPI dependency that enforces *role*.

    ``admin`` always passes.  Otherwise the user must hold *role*
    exactly.

    Usage::

        @router.post("/api/v1/sync")
        async def trigger_sync(user: AuthenticatedUser = Depends(require_role("sync:write"))):
            ...

    Raises:
        HTTPException(403): if the user does not have the required role.
    """

    async def _enforcer(
        user: AuthenticatedUser = Depends(get_current_user),
    ) -> AuthenticatedUser:
        if not user.has_role(role):
            raise HTTPException(
                status_code=403,
                detail=f"Role '{role}' required",
            )
        return user

    return _enforcer


def require_any_role(*roles: str) -> Callable:
    """Return a FastAPI dependency that passes if the user holds *any* of the listed roles.

    ``admin`` always passes.

    Raises:
        HTTPException(403): if the user holds none of the listed roles.
    """

    async def _enforcer(
        user: AuthenticatedUser = Depends(get_current_user),
    ) -> AuthenticatedUser:
        if not user.has_any_role(*roles):
            raise HTTPException(
                status_code=403,
                detail=f"One of roles {list(roles)} required",
            )
        return user

    return _enforcer
