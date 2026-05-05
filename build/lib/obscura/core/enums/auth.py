"""Auth and capability enums: permission modes, capability tiers, providers."""

from __future__ import annotations

from enum import StrEnum


class PermissionMode(StrEnum):
    """Named permission modes controlling tool execution gating."""

    DEFAULT = "default"
    PLAN = "plan"
    ACCEPT_EDITS = "accept_edits"
    BYPASS = "bypass"


class CapabilityTier(StrEnum):
    """The two tiers of capability access.

    - ``PUBLIC``     -- Tier A: minimal tools, strict prompt filters.
    - ``PRIVILEGED`` -- Tier B: full tools, debug, relaxed filters.
    """

    PUBLIC = "public"
    PRIVILEGED = "privileged"


class AuthProvider(StrEnum):
    """OAuth identity provider for CLI session sync."""

    GITHUB = "github"
    GOOGLE = "google"
