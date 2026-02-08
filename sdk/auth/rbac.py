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
import os

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

# API Keys - loaded from env var OBSCURA_API_KEYS (comma-separated key:name:role1,role2)
# Example: OBSCURA_API_KEYS="key1:dev-user:admin,agent:copilot;key2:readonly-user:agent:read"
_API_KEYS: dict[str, dict] = {}

def _load_api_keys():
    """Load API keys from environment."""
    global _API_KEYS
    keys_env = os.environ.get("OBSCURA_API_KEYS", "")
    if not keys_env:
        # Default test key for convenience
        _API_KEYS = {
            "obscura-dev-key-123": {
                "user_id": "dev-user",
                "email": "dev@obscura.local",
                "roles": ["admin", "agent:copilot", "agent:claude", "agent:read", "sync:write", "sessions:manage"]
            }
        }
        return
    
    _API_KEYS = {}
    for key_def in keys_env.split(";"):
        parts = key_def.split(":")
        if len(parts) >= 3:
            key, user_id, roles_str = parts[0], parts[1], parts[2]
            roles = roles_str.split(",") if roles_str else ["agent:read"]
            _API_KEYS[key] = {
                "user_id": user_id,
                "email": f"{user_id}@obscura.local",
                "roles": roles
            }

_load_api_keys()


# ---------------------------------------------------------------------------
# Core dependency: extract user from request
# ---------------------------------------------------------------------------

async def get_current_user(request: Request) -> AuthenticatedUser:
    """FastAPI dependency that returns the authenticated user.

    Checks in order:
    1. X-API-Key header (if OBSCURA_AUTH_ENABLED=true)
    2. JWT token (from middleware, set in request.state.user)
    3. Mock user (if auth is disabled)

    Raises:
        HTTPException(401): if no valid auth found.
    """
    # Check if auth is disabled via app config
    config = getattr(request.app.state, "config", None)
    auth_enabled = getattr(config, "auth_enabled", True) if config else True
    
    # 1. Check API key header (works even when auth is enabled)
    api_key = request.headers.get("X-API-Key")
    if api_key and api_key in _API_KEYS:
        key_data = _API_KEYS[api_key]
        return AuthenticatedUser(
            user_id=key_data["user_id"],
            email=key_data["email"],
            roles=tuple(key_data["roles"]),
            org_id="local",
            token_type="api_key",
            raw_token=api_key
        )
    
    # 2. If auth is disabled, return mock user
    if not auth_enabled:
        return _MOCK_USER
    
    # 3. Check JWT token from middleware
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
