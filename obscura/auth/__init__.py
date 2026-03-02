"""
obscura.auth -- Authentication and authorization for the Obscura platform.

Re-exports the public API so consumers can write::

    from obscura.auth import AuthenticatedUser, APIKeyAuthMiddleware, get_current_user, require_role
"""

from __future__ import annotations

from obscura.auth.capability import (
    CapabilityTier,
    CapabilityToken,
    generate_capability_token,
    resolve_tier,
    validate_capability_token,
)
from obscura.auth.middleware import APIKeyAuthMiddleware
from obscura.auth.models import VALID_ROLES, AuthenticatedUser
from obscura.auth.prompt_filter import FilterResult, filter_prompt
from obscura.auth.rbac import get_current_user, require_any_role, require_role
from obscura.auth.system_prompts import get_tier_system_prompt

__all__ = [
    # Models
    "AuthenticatedUser",
    "VALID_ROLES",
    # Capability system
    "CapabilityTier",
    "CapabilityToken",
    "generate_capability_token",
    "validate_capability_token",
    "resolve_tier",
    "filter_prompt",
    "FilterResult",
    "get_tier_system_prompt",
    # Middleware
    "APIKeyAuthMiddleware",
    # RBAC
    "get_current_user",
    "require_role",
    "require_any_role",
]
