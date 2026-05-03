"""obscura.auth.models -- Data models for authentication and identity.

Provides the AuthenticatedUser type that is set on request.state.user
by the JWT validation middleware and consumed by RBAC dependencies.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, field_validator

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
        "agent:codex",
        "agent:moonshot",
        "agent:read",
        "sync:write",
        "sessions:manage",
        "a2a:invoke",
        "a2a:manage",
    },
)


# ---------------------------------------------------------------------------
# Authenticated user
# ---------------------------------------------------------------------------


class AuthenticatedUser(BaseModel):
    """Represents a validated user extracted from a JWT.

    Populated by :mod:`obscura.auth.middleware` and attached to
    ``request.state.user`` for downstream handlers.
    """

    model_config = ConfigDict(frozen=True)

    user_id: str
    email: str
    roles: tuple[str, ...]
    org_id: str | None
    token_type: str  # "user" | "service" | "api_key"
    raw_token: str
    # Supabase Authenticator Assurance Level — ``aal1`` = password/OAuth only,
    # ``aal2`` = MFA completed. ``None`` for non-Supabase credentials
    # (API keys, mock users).
    aal: str | None = None
    # Supabase session_id (JWT ``session_id`` claim). Used for revocation
    # checks against ``auth.sessions`` on sensitive routes.
    session_id: str | None = None

    @field_validator("roles", mode="after")
    @classmethod
    def _expand_admin(cls, roles: tuple[str, ...]) -> tuple[str, ...]:
        """Admin grants every role. Expand the tuple at construction time so
        direct ``.roles`` consumers (telemetry, API responses, capability
        checks) see the full set, not just ``("admin",)``.
        """
        if "admin" not in roles:
            return roles
        # Preserve admin first, then append every other valid role in a
        # stable order so equality / hashing is deterministic.
        rest = tuple(sorted(VALID_ROLES - {"admin"}))
        return ("admin", *rest)

    # -- convenience helpers ------------------------------------------------

    def has_role(self, role: str) -> bool:
        """Check whether the user holds *role* (admin always passes)."""
        return "admin" in self.roles or role in self.roles

    def has_any_role(self, *roles: str) -> bool:
        """Return True if the user holds at least one of *roles*."""
        return "admin" in self.roles or bool(set(self.roles) & set(roles))
