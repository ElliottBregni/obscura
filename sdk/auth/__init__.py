"""
sdk.auth -- Authentication and authorization for the Obscura platform.

Re-exports the public API so consumers can write::

    from sdk.auth import AuthenticatedUser, JWTAuthMiddleware, get_current_user, require_role
"""

from __future__ import annotations

from sdk.auth.middleware import JWKSCache, JWTAuthMiddleware, decode_and_validate
from sdk.auth.models import VALID_ROLES, AuthenticatedUser
from sdk.auth.rbac import get_current_user, require_any_role, require_role
from sdk.auth.zitadel import ZitadelClient, bootstrap

__all__ = [
    # Models
    "AuthenticatedUser",
    "VALID_ROLES",
    # Middleware
    "JWTAuthMiddleware",
    "JWKSCache",
    "decode_and_validate",
    # RBAC
    "get_current_user",
    "require_role",
    "require_any_role",
    # Zitadel
    "ZitadelClient",
    "bootstrap",
]
