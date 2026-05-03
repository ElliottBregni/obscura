"""obscura.auth.models -- Data models for authentication and identity.

Provides the AuthenticatedUser type that is set on request.state.user
by the JWT validation middleware and consumed by RBAC dependencies.
"""

from __future__ import annotations

import os

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

    # -- factory constructors ----------------------------------------------

    @classmethod
    def local_cli(cls) -> AuthenticatedUser:
        """Return the synthetic user used by CLI/daemon code paths.

        There is no real authentication in standalone tooling — vector memory,
        profile store, and goal board still need a stable ``user_id`` to
        namespace per-user state, so we mint a placeholder identity from
        ``$USER`` (falling back to ``"local"``).
        """
        return cls(
            user_id=os.environ.get("USER", "local"),
            email="cli@obscura.local",
            roles=("operator",),
            org_id="local",
            token_type="user",
            raw_token="",
        )

    @classmethod
    def from_tool_context(cls) -> AuthenticatedUser:
        """Return the user bound to the active tool context, or the CLI fallback.

        Tool handlers should prefer this over :meth:`local_cli` so that
        server-side invocations (where the agent loop binds an authenticated
        user into the :class:`~obscura.core.tool_context.ToolContext`) get the
        real user, while CLI/daemon paths transparently fall back to the
        local synthetic identity.
        """
        from obscura.core.tool_context import current_tool_context

        ctx = current_tool_context()
        if ctx is not None and isinstance(ctx.user, cls):
            return ctx.user
        return cls.local_cli()

    # -- convenience helpers ------------------------------------------------

    def has_role(self, role: str) -> bool:
        """Check whether the user holds *role* (admin always passes)."""
        return "admin" in self.roles or role in self.roles

    def has_any_role(self, *roles: str) -> bool:
        """Return True if the user holds at least one of *roles*."""
        return "admin" in self.roles or bool(set(self.roles) & set(roles))
