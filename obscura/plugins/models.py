"""Normalized resource models for the Obscura plugin platform.

Every model is a frozen dataclass with a ``version`` field. These are the
internal representations that the loader produces and the runtime registries
consume. Plugins describe their contributions via manifests; the loader
validates and converts manifest entries into these models.

Design principles:
- Immutable after creation (frozen dataclasses).
- Every model carries a version for upgrade/compat tracking.
- Tuple fields for immutable collections (not lists).
- Validation helpers raise ``ValueError`` on invalid data.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

_SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?"
    r"(?:\+([0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?$",
)

_CAPABILITY_ID_RE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")

_PLUGIN_ID_RE = re.compile(r"^[a-z][a-z0-9_-]*$")


def validate_semver(version: str) -> str:
    """Return *version* if it matches semver, else raise ``ValueError``."""
    if not _SEMVER_RE.match(version):
        msg = f"Invalid semver: {version!r}"
        raise ValueError(msg)
    return version


def validate_capability_id(cap_id: str) -> str:
    """Return *cap_id* if it follows ``domain.action`` convention."""
    if not _CAPABILITY_ID_RE.match(cap_id):
        msg = (
            f"Invalid capability ID {cap_id!r} — must be dot-separated "
            f"lowercase (e.g. 'repo.read', 'shell.exec')"
        )
        raise ValueError(
            msg,
        )
    return cap_id


def validate_plugin_id(plugin_id: str) -> str:
    """Return *plugin_id* if it follows kebab/snake lowercase convention."""
    if not _PLUGIN_ID_RE.match(plugin_id):
        msg = (
            f"Invalid plugin ID {plugin_id!r} — must be lowercase "
            f"alphanumeric with hyphens/underscores"
        )
        raise ValueError(
            msg,
        )
    return plugin_id


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BootstrapDep:
    """A single dependency that must be present for a plugin to work."""

    type: str  # "pip" | "uv" | "npx" | "binary" | "npm" | "cargo" | "brew" | "pipx"
    package: str  # package name or binary name
    version: str = ""  # version constraint (e.g. ">=1.0.0")
    optional: bool = False

    def __post_init__(self) -> None:
        if self.type not in (
            "pip",
            "uv",
            "npx",
            "binary",
            "npm",
            "cargo",
            "brew",
            "pipx",
        ):
            msg = (
                f"Unknown bootstrap dep type: {self.type!r} — "
                f"must be one of: pip, uv, npx, binary, npm, cargo, brew, pipx"
            )
            raise ValueError(
                msg,
            )
        if not self.package.strip():
            msg = "Bootstrap dep package must not be empty"
            raise ValueError(msg)


@dataclass(frozen=True)
class BootstrapSpec:
    """Declares how to install a plugin's runtime dependencies.

    The loader runs bootstrap before handler resolution. Each dep is
    checked (already installed?) and installed if missing.
    """

    deps: tuple[BootstrapDep, ...] = field(default_factory=tuple)
    post_install: str = ""  # optional shell command to run after deps installed
    check_command: str = (
        ""  # command to verify bootstrap succeeded (e.g. "gws --version")
    )
    tools_module: str = (
        ""  # relative module path with tool handlers (e.g. "tools.tools")
    )
    tools_list: str = (
        ""  # attribute in tools_module exporting specs (e.g. "TOOL_SPECS")
    )


# ---------------------------------------------------------------------------
# Healthcheck
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HealthcheckSpec:
    """How the runtime verifies a plugin is still functional."""

    type: str  # "callable" | "http" | "binary" | "python_import"
    target: str  # dotted path, URL, or binary name
    interval_seconds: int = 300

    def __post_init__(self) -> None:
        if self.type not in ("callable", "http", "binary", "python_import"):
            msg = f"Unknown healthcheck type: {self.type!r}"
            raise ValueError(msg)
        if self.interval_seconds < 1:
            msg = "interval_seconds must be >= 1"
            raise ValueError(msg)


# ---------------------------------------------------------------------------
# Policy Hint
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PolicyHintSpec:
    """Advisory access recommendation from a plugin."""

    capability_id: str
    recommended_action: str  # "allow" | "deny" | "approve"
    reason: str = ""

    def __post_init__(self) -> None:
        validate_capability_id(self.capability_id)
        if self.recommended_action not in ("allow", "deny", "approve"):
            msg = f"Invalid recommended_action: {self.recommended_action!r}"
            raise ValueError(
                msg,
            )


# ---------------------------------------------------------------------------
# Instruction Overlay
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InstructionSpec:
    """Guidance text a plugin contributes to agent system prompts."""

    id: str
    version: str
    scope: str  # "global" | "agent" | "session"
    content: str
    priority: int = 50  # lower = earlier in assembled prompt

    def __post_init__(self) -> None:
        validate_semver(self.version)
        if self.scope not in ("global", "agent", "session"):
            msg = f"Invalid instruction scope: {self.scope!r}"
            raise ValueError(msg)
        if not self.content.strip():
            msg = "Instruction content must not be empty"
            raise ValueError(msg)


# ---------------------------------------------------------------------------
# Capability
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CapabilitySpec:
    """A named, permissioned feature surface gating one or more tools."""

    id: str
    version: str
    description: str
    tools: tuple[str, ...] = field(default_factory=tuple)
    requires_approval: bool = False
    default_grant: bool = True

    def __post_init__(self) -> None:
        validate_capability_id(self.id)
        validate_semver(self.version)


# ---------------------------------------------------------------------------
# Workflow
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkflowSpec:
    """A composed, multi-step behavior built from tools and capabilities."""

    id: str
    version: str
    name: str
    description: str
    steps: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    required_capabilities: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        validate_semver(self.version)
        for cap_id in self.required_capabilities:
            validate_capability_id(cap_id)


# ---------------------------------------------------------------------------
# Tool contribution (thin wrapper referencing ToolSpec fields)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolContribution:
    """A tool declared in a plugin manifest (pre-ToolSpec normalization).

    The loader converts these into full ``ToolSpec`` instances during the
    normalize step. ``handler_ref`` is a dotted import path resolved lazily.
    """

    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=dict[str, Any])
    handler_ref: str = ""  # e.g. "my_plugin.tools:search_repo"
    capability: str = ""  # capability ID this tool belongs to
    side_effects: str = "none"  # "none" | "read" | "write"
    required_tier: str = "public"
    timeout_seconds: float = 60.0
    retries: int = 0

    def __post_init__(self) -> None:
        if self.side_effects not in ("none", "read", "write"):
            msg = f"Invalid side_effects: {self.side_effects!r}"
            raise ValueError(msg)


# ---------------------------------------------------------------------------
# Config requirement
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConfigRequirement:
    """A configuration value a plugin needs to function."""

    key: str  # env var name or config path
    type: str = "string"  # "string" | "int" | "bool" | "secret"
    required: bool = True
    description: str = ""
    default: str | None = None


# ---------------------------------------------------------------------------
# Plugin Spec — the top-level normalized resource
# ---------------------------------------------------------------------------

# Valid source types
SOURCE_TYPES = frozenset(
    {
        "local",
        "git",
        "pip",
        "builtin",
        "npm",
        "cargo",
        "uv",
        "registry",
    },
)

# Valid runtime types
RUNTIME_TYPES = frozenset(
    {
        "native",
        "cli",
        "sdk",
        "mcp",
        "service",
        "content",
        "npx",
        "wasm",
        "docker",
        "grpc",
    },
)

# Valid trust levels (ordered from most to least trusted)
TRUST_LEVELS = ("builtin", "verified", "community", "untrusted")


@dataclass(frozen=True)
class PluginSpec:
    """Complete normalized description of an Obscura plugin.

    Produced by the manifest parser/validator. Consumed by the loader
    and runtime registries. Immutable after creation.
    """

    id: str
    name: str
    version: str
    source_type: str  # SOURCE_TYPES
    runtime_type: str  # RUNTIME_TYPES
    trust_level: str = "community"  # TRUST_LEVELS
    author: str = ""
    description: str = ""
    source_dir: Path | None = None  # directory containing the manifest

    # Config
    config_requirements: tuple[ConfigRequirement, ...] = field(default_factory=tuple)

    # Contributed resources
    capabilities: tuple[CapabilitySpec, ...] = field(default_factory=tuple)
    tools: tuple[ToolContribution, ...] = field(default_factory=tuple)
    workflows: tuple[WorkflowSpec, ...] = field(default_factory=tuple)
    instructions: tuple[InstructionSpec, ...] = field(default_factory=tuple)
    policy_hints: tuple[PolicyHintSpec, ...] = field(default_factory=tuple)

    # Lifecycle hooks (dotted import paths)
    install_hook: str | None = None
    bootstrap_hook: str | None = None

    # Bootstrap (dependency installation)
    bootstrap: BootstrapSpec | None = None

    # Health
    healthcheck: HealthcheckSpec | None = None

    def __post_init__(self) -> None:
        validate_plugin_id(self.id)
        validate_semver(self.version)
        if self.source_type not in SOURCE_TYPES:
            msg = (
                f"Invalid source_type {self.source_type!r} — "
                f"must be one of {sorted(SOURCE_TYPES)}"
            )
            raise ValueError(
                msg,
            )
        if self.runtime_type not in RUNTIME_TYPES:
            msg = (
                f"Invalid runtime_type {self.runtime_type!r} — "
                f"must be one of {sorted(RUNTIME_TYPES)}"
            )
            raise ValueError(
                msg,
            )
        if self.trust_level not in TRUST_LEVELS:
            msg = (
                f"Invalid trust_level {self.trust_level!r} — "
                f"must be one of {list(TRUST_LEVELS)}"
            )
            raise ValueError(
                msg,
            )

    # -- Convenience -------------------------------------------------------

    @property
    def tool_names(self) -> tuple[str, ...]:
        """All tool names contributed by this plugin."""
        return tuple(t.name for t in self.tools)

    @property
    def capability_ids(self) -> tuple[str, ...]:
        """All capability IDs contributed by this plugin."""
        return tuple(c.id for c in self.capabilities)

    @property
    def workflow_ids(self) -> tuple[str, ...]:
        """All workflow IDs contributed by this plugin."""
        return tuple(w.id for w in self.workflows)


# ---------------------------------------------------------------------------
# Lifecycle state (mutable — used by registry/loader, not in specs)
# ---------------------------------------------------------------------------


@dataclass
class PluginStatus:
    """Mutable lifecycle state for an installed plugin."""

    plugin_id: str
    state: str = (
        "discovered"  # discovered|installed|enabled|active|unhealthy|disabled|failed
    )
    error: str | None = None
    installed_at: str | None = None
    updated_at: str | None = None
    enabled: bool = False

    _VALID_STATES = frozenset(
        {
            "discovered",
            "installed",
            "enabled",
            "active",
            "unhealthy",
            "disabled",
            "failed",
        },
    )

    def __post_init__(self) -> None:
        if self.state not in self._VALID_STATES:
            msg = f"Invalid plugin state: {self.state!r}"
            raise ValueError(msg)


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

__all__ = [
    "RUNTIME_TYPES",
    "SOURCE_TYPES",
    "TRUST_LEVELS",
    "BootstrapDep",
    "BootstrapSpec",
    "CapabilitySpec",
    "ConfigRequirement",
    "HealthcheckSpec",
    "InstructionSpec",
    "PluginSpec",
    "PluginStatus",
    "PolicyHintSpec",
    "ToolContribution",
    "WorkflowSpec",
    "validate_capability_id",
    "validate_plugin_id",
    "validate_semver",
]
