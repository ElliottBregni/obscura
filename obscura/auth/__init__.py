"""obscura.auth -- Authentication and authorization for the Obscura platform.

Re-exports the public API so consumers can write::

    from obscura.auth import AuthenticatedUser, APIKeyAuthMiddleware, get_current_user, require_role

The middleware/RBAC symbols are imported lazily via ``__getattr__`` because
they pull in FastAPI/starlette — dependencies the CLI doesn't need.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from obscura.auth.capability import (
    CapabilityTier,
    CapabilityToken,
    generate_capability_token,
    resolve_tier,
    validate_capability_token,
)
from obscura.auth.cli_user import local_cli_user
from obscura.auth.models import VALID_ROLES, AuthenticatedUser
from obscura.auth.prompt_filter import FilterResult, filter_prompt
from obscura.auth.system_prompts import get_tier_system_prompt

if TYPE_CHECKING:
    from obscura.auth.middleware import APIKeyAuthMiddleware
    from obscura.auth.rbac import get_current_user, require_any_role, require_role

__all__ = [
    "VALID_ROLES",
    # Middleware
    "APIKeyAuthMiddleware",
    # Models
    "AuthenticatedUser",
    # Capability system
    "CapabilityTier",
    "CapabilityToken",
    "FilterResult",
    "filter_prompt",
    "generate_capability_token",
    # RBAC
    "get_current_user",
    "get_tier_system_prompt",
    "local_cli_user",
    "require_any_role",
    "require_role",
    "resolve_tier",
    "validate_capability_token",
]


# Lazy imports — fastapi/starlette only loaded when these are accessed.
def __getattr__(name: str) -> object:
    if name == "APIKeyAuthMiddleware":
        from obscura.auth.middleware import APIKeyAuthMiddleware

        return APIKeyAuthMiddleware
    if name in ("get_current_user", "require_any_role", "require_role"):
        from obscura.auth import rbac

        return getattr(rbac, name)
    msg = f"module 'obscura.auth' has no attribute {name!r}"
    raise AttributeError(msg)
