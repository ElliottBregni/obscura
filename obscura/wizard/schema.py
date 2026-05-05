"""Pydantic models for wizard-managed config.

These are the boundary types shared by the TUI, the FastAPI router, and the
MCP tools. ``extra="forbid"`` on the inputs keeps the wizard's surface
narrow — unrelated keys in ``config.toml`` are preserved by the service
but never round-trip through these models.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class Profile(BaseModel):
    """A named bundle of runtime settings that compose a system prompt."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1)
    prompts: list[str] = Field(default_factory=list)
    backend: str | None = None
    model: str | None = None
    mode: str | None = Field(
        default=None,
        description='Tool-loading mode: "code" | "ask" | "plan" | "diff" | None.',
    )
    capabilities: list[str] = Field(default_factory=list)
    plugins: list[str] = Field(default_factory=list)
    mcp_servers: list[str] = Field(default_factory=list)
    agents: list[str] = Field(default_factory=list)
    skills: list[str] = Field(
        default_factory=list,
        description="Skill names to load from ~/.obscura/skills/. Empty = no filter (load all).",
    )
    vault_path: str | None = Field(
        default=None,
        description="Override for the Obsidian-style knowledge vault path.",
    )


class WorkspaceBinding(BaseModel):
    """Per-cwd profile override (matched against the resolved working dir)."""

    model_config = ConfigDict(extra="forbid")

    path: str
    profile: str


class ActiveState(BaseModel):
    """Which profile is active when no workspace override matches."""

    model_config = ConfigDict(extra="forbid")

    profile: str = "default"


class WizardSnapshot(BaseModel):
    """Read-only view of everything the wizard knows about.

    The ``available_*`` fields are derived from the filesystem + registry
    state; they are not stored in ``config.toml``.
    """

    model_config = ConfigDict(extra="forbid")

    profiles: list[Profile]
    active: ActiveState
    workspaces: list[WorkspaceBinding]
    available_prompts: list[str]
    available_capabilities: list[str]
    available_plugins: list[str]
    available_backends: list[str]
    available_mcp_servers: list[str]
    available_agents: list[str]
    available_skills: list[str] = Field(default_factory=list)
    available_modes: list[str] = Field(default_factory=list)
    available_commands: list[str] = Field(default_factory=list)
    hooks_summary: dict[str, int] = Field(
        default_factory=dict,
        description="Map of hook event -> count, derived from ~/.obscura/hooks/hooks.json.",
    )
    default_vault_path: str = Field(
        default="",
        description="Filesystem path to the default vault (~/.obscura/vault by default).",
    )
    soul_path: str = Field(
        default="",
        description="Filesystem path to ~/.obscura/SOUL.md (whether or not it exists).",
    )
