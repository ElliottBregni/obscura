"""
sdk.auth.models -- Data models for authentication and identity.

Provides the AuthenticatedUser type that is set on request.state.user
by the JWT validation middleware and consumed by RBAC dependencies.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------

VALID_ROLES: frozenset[str] = frozenset(
    {
        "admin",
        "operator",
        "tier:privileged",
        "agent:copilot",
        "agent:claude",
        "agent:localllm",
        "agent:openai",
        "agent:read",
        "sync:write",
        "sessions:manage",
        "a2a:invoke",
        "a2a:manage",
    }
)


# ---------------------------------------------------------------------------
# Authenticated user
# ---------------------------------------------------------------------------


class AuthenticatedUser(BaseModel):
    """Represents a validated user extracted from a JWT.

    Populated by :mod:`sdk.auth.middleware` and attached to
    ``request.state.user`` for downstream handlers.
    """

    model_config = ConfigDict(frozen=True)

    user_id: str
    email: str
    roles: tuple[str, ...]
    org_id: str | None
    token_type: str  # "user" | "service" | "api_key"
    raw_token: str

    # -- convenience helpers ------------------------------------------------

    def has_role(self, role: str) -> bool:
        """Check whether the user holds *role* (admin always passes)."""
        return "admin" in self.roles or role in self.roles

    def has_any_role(self, *roles: str) -> bool:
        """Return True if the user holds at least one of *roles*."""
        return "admin" in self.roles or bool(set(self.roles) & set(roles))
