"""obscura.auth.rbac -- Role-based access control dependencies for FastAPI.

Provides ``Depends()``-compatible callables for extracting the current
user and enforcing role requirements on individual endpoints.

Roles
-----
- ``admin``           -- full access, bypasses all role checks
- ``agent:copilot``   -- may invoke the Copilot backend
- ``agent:claude``    -- may invoke the Claude backend
- ``agent:localllm``  -- may invoke the LocalLLM backend
- ``agent:openai``    -- may invoke the OpenAI backend
- ``agent:codex``     -- may invoke the Codex backend
- ``agent:moonshot``  -- may invoke the Moonshot/Kimi backend
- ``agent:read``      -- read-only agent access (send / stream)
- ``sync:write``      -- trigger vault sync
- ``sessions:manage`` -- create / delete sessions
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from fastapi import Depends, HTTPException, Request

from obscura.auth.models import AuthenticatedUser

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

# ---------------------------------------------------------------------------
# Role constants — use these in route ``Depends()`` calls to keep role
# lists in sync when new backends are added.
# ---------------------------------------------------------------------------

AGENT_WRITE_ROLES = (
    "agent:copilot",
    "agent:claude",
    "agent:localllm",
    "agent:openai",
    "agent:codex",
    "agent:moonshot",
)
"""Roles that may spawn, run, stop, or mutate agents."""

AGENT_READ_ROLES = (*AGENT_WRITE_ROLES, "agent:read")
"""Roles that may send prompts or read agent state."""

# API Keys - loaded from env var OBSCURA_API_KEYS (comma-separated key:name:role1,role2)
# Example: OBSCURA_API_KEYS="key1:dev-user:admin,agent:copilot;key2:readonly-user:agent:read"
_api_keys: dict[str, dict[str, str | list[str]]] = {}


def _dev_mode_enabled() -> bool:
    """The dev API key only loads when explicitly opted into."""
    return os.environ.get("OBSCURA_DEV_MODE", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _load_api_keys() -> None:
    """Load API keys from environment.

    Security: plaintext keys are hashed (SHA-256) on load and never kept in
    memory. The dev fallback key loads ONLY when ``OBSCURA_DEV_MODE=true`` —
    this prevents accidental production shipment of the publicly-known key.
    """
    global _api_keys
    keys_env = os.environ.get("OBSCURA_API_KEYS", "")

    if not keys_env:
        if _dev_mode_enabled():
            logger.warning(
                "OBSCURA_DEV_MODE=true — loading public dev API key "
                "'obscura-dev-key-123' with VIEWER-ONLY access. "
                "NEVER set this in production.",
            )
            _api_keys = {
                _hash_api_key("obscura-dev-key-123"): {
                    "user_id": "dev-user",
                    "email": "dev@obscura.local",
                    "roles": ["agent:read"],
                },
            }
        else:
            logger.info(
                "OBSCURA_API_KEYS not set and OBSCURA_DEV_MODE is off — "
                "API-key path disabled. Authenticate via Supabase OAuth.",
            )
            _api_keys = {}
        return

    _api_keys = {}
    for key_def in keys_env.split(";"):
        parts = key_def.split(":")
        if len(parts) >= 3:
            key, user_id, roles_str = parts[0], parts[1], parts[2]
            roles = roles_str.split(",") if roles_str else ["agent:read"]
            _api_keys[_hash_api_key(key)] = {
                "user_id": user_id,
                "email": f"{user_id}@obscura.local",
                "roles": roles,
            }


def _hash_api_key(raw: str) -> str:
    """Hash a raw API key for storage. SHA-256 is fine here — keys are
    high-entropy random strings, not passwords, so a slow KDF adds latency
    without meaningful resistance improvement.
    """
    import hashlib

    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


_load_api_keys()


def user_from_api_key(api_key: str | None) -> AuthenticatedUser | None:
    """Return an authenticated API key user, or ``None`` when key is invalid.

    Lookup is hash-based (plaintext keys are never stored in memory).
    Comparison uses ``secrets.compare_digest`` to avoid leaking key bits
    through timing differences.
    """
    if not api_key:
        return None

    import secrets as _secrets

    candidate_hash = _hash_api_key(api_key)
    # Walk every stored hash to keep lookup time independent of whether
    # a match exists or where in the dict it lives. O(n) over the API-key
    # set — fine for typical sizes (dozens), matters for timing safety.
    matched: dict[str, str | list[str]] | None = None
    for stored_hash, data in _api_keys.items():
        if _secrets.compare_digest(stored_hash, candidate_hash):
            matched = data
    if matched is None:
        return None

    user_id = str(matched["user_id"])
    email = str(matched["email"])
    roles_val = matched["roles"]
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
    1. X-API-Key header
    2. User from middleware (set in request.state.user)

    Raises:
        HTTPException(401): if no valid auth found.

    """
    # 1. Check API key header
    api_user = user_from_api_key(request.headers.get("X-API-Key"))
    if api_user is not None:
        return api_user

    # 2. Check JWT token from middleware
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


def require_mfa() -> Callable[..., Awaitable[AuthenticatedUser]]:
    """Return a FastAPI dependency that enforces MFA (AAL2) for the caller.

    Supabase-authenticated users pass only when their JWT carries
    ``aal=aal2`` (they completed an MFA challenge). API-key callers and
    mock users pass through (no MFA concept — enforced at the network /
    operator layer instead).

    Usage::

        @router.post("/api/v1/admin/promote")
        async def promote(
            user: AuthenticatedUser = Depends(require_mfa()),
        ): ...
    """

    async def _enforcer(
        user: AuthenticatedUser = Depends(get_current_user),
    ) -> AuthenticatedUser:
        # API keys and mock users skip MFA — they're machine credentials.
        if user.token_type != "user":
            return user
        if user.aal != "aal2":
            raise HTTPException(
                status_code=403,
                detail="MFA required for this action (AAL2)",
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
