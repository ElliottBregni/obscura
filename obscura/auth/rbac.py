"""
obscura.auth.rbac -- Role-based access control dependencies for FastAPI.

Provides ``Depends()``-compatible callables for extracting the current
user and enforcing role requirements on individual endpoints.

Roles
-----
- ``admin``           -- full access, bypasses all role checks
- ``agent:copilot``   -- may invoke the Copilot backend
- ``agent:claude``    -- may invoke the Claude backend
- ``agent:localllm``  -- may invoke the LocalLLM backend
- ``agent:openai``    -- may invoke the OpenAI backend
- ``agent:moonshot``  -- may invoke the Moonshot/Kimi backend
- ``agent:read``      -- read-only agent access (send / stream)
- ``sync:write``      -- trigger vault sync
- ``sessions:manage`` -- create / delete sessions
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable

from fastapi import Depends, HTTPException, Request

from obscura.auth.models import AuthenticatedUser

# ---------------------------------------------------------------------------
# Role constants — use these in route ``Depends()`` calls to keep role
# lists in sync when new backends are added.
# ---------------------------------------------------------------------------

AGENT_WRITE_ROLES = (
    "agent:copilot",
    "agent:claude",
    "agent:localllm",
    "agent:openai",
    "agent:moonshot",
)
"""Roles that may spawn, run, stop, or mutate agents."""

AGENT_READ_ROLES = (*AGENT_WRITE_ROLES, "agent:read")
"""Roles that may send prompts or read agent state."""

# Mock user for when auth is disabled
_MOCK_USER = AuthenticatedUser(
    user_id="anonymous",
    email="anonymous@obscura.local",
    roles=("admin", *AGENT_WRITE_ROLES, "agent:read", "sync:write", "sessions:manage"),
    org_id="local",
    token_type="anonymous",
    raw_token="",
)

# API Keys - loaded from env var OBSCURA_API_KEYS (comma-separated key:name:role1,role2)
# Example: OBSCURA_API_KEYS="key1:dev-user:admin,agent:copilot;key2:readonly-user:agent:read"
_api_keys: dict[str, dict[str, str | list[str]]] = {}


def _load_api_keys() -> None:
    """Load API keys from environment."""
    global _api_keys
    keys_env = os.environ.get("OBSCURA_API_KEYS", "")
    if not keys_env:
        # Default test key for convenience
        _api_keys = {
            "obscura-dev-key-123": {
                "user_id": "dev-user",
                "email": "dev@obscura.local",
                "roles": [
                    "admin",
                    *AGENT_WRITE_ROLES,
                    "agent:read",
                    "sync:write",
                    "sessions:manage",
                ],
            }
        }
        return

    _api_keys = {}
    for key_def in keys_env.split(";"):
        parts = key_def.split(":")
        if len(parts) >= 3:
            key, user_id, roles_str = parts[0], parts[1], parts[2]
            roles = roles_str.split(",") if roles_str else ["agent:read"]
            _api_keys[key] = {
                "user_id": user_id,
                "email": f"{user_id}@obscura.local",
                "roles": roles,
            }


_load_api_keys()


def user_from_api_key(api_key: str | None) -> AuthenticatedUser | None:
    """Return an authenticated API key user, or ``None`` when key is invalid."""
    if not api_key:
        return None
    key_data = _api_keys.get(api_key)
    if not key_data:
        return None
    user_id = str(key_data["user_id"])
    email = str(key_data["email"])
    roles_val = key_data["roles"]
    roles = tuple(roles_val) if isinstance(roles_val, list) else (str(roles_val),)
    return AuthenticatedUser(
        user_id=user_id,
        email=email,
        roles=roles,
        org_id="local",
        token_type="api_key",
        raw_token=api_key,
    )


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
    api_user = user_from_api_key(request.headers.get("X-API-Key"))
    if api_user is not None:
        return api_user

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


def require_role(role: str) -> Callable[..., Awaitable[AuthenticatedUser]]:
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


def require_any_role(*roles: str) -> Callable[..., Awaitable[AuthenticatedUser]]:
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
