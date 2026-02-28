"""obscura.manifest.models — Pydantic models for agent manifests.

These models represent the structured data extracted from markdown files
with YAML frontmatter.  They are pure data — no file I/O or resolution
logic lives here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, Field


def _empty_str_list() -> list[str]:
    return []


def _empty_dict() -> dict[str, str]:
    return {}


class PermissionConfig(BaseModel):
    """Permission block from settings.json or agent frontmatter."""

    allow: list[str] = Field(default_factory=_empty_str_list)
    deny: list[str] = Field(default_factory=_empty_str_list)


class HookDefinition(BaseModel):
    """A single hook entry from hooks.json or agent frontmatter.

    ``event`` maps to a lifecycle moment (preToolUse, postToolUse, etc.).
    ``type`` is ``"command"`` for shell commands or ``"python"`` for
    importable callables.
    """

    type: str = "command"
    event: str
    bash: str = ""
    module: str = ""
    timeout_sec: int = 10
    comment: str = ""


class SkillManifest(BaseModel):
    """Parsed skill from a SKILL.md or skill markdown file."""

    name: str
    description: str = ""
    user_invocable: bool = True
    allowed_tools: list[str] = Field(default_factory=_empty_str_list)
    body: str = ""
    source_path: Path | None = None

    model_config = {"arbitrary_types_allowed": True}


class InstructionManifest(BaseModel):
    """Parsed instruction file with optional applyTo globs."""

    apply_to: list[str] = Field(default_factory=_empty_str_list)
    body: str = ""
    source_path: Path | None = None

    model_config = {"arbitrary_types_allowed": True}


class MCPServerRef(BaseModel):
    """MCP server reference from agent frontmatter or servers.json."""

    name: str
    transport: str = "stdio"
    command: str = ""
    args: list[str] = Field(default_factory=_empty_str_list)
    env: dict[str, str] = Field(default_factory=_empty_dict)
    url: str = ""
    description: str = ""


def _empty_hook_list() -> list[HookDefinition]:
    return []


def _empty_skill_list() -> list[SkillManifest]:
    return []


def _empty_instruction_list() -> list[InstructionManifest]:
    return []


def _empty_mcp_refs() -> list[MCPServerRef]:
    return []


class AgentManifest(BaseModel):
    """Complete parsed agent manifest from an ``*.agent.md`` file.

    YAML frontmatter maps to structured config fields.
    The markdown body becomes ``system_prompt``.
    """

    # Identity
    name: str
    description: str = ""
    model: str = "copilot"

    # System prompt (from markdown body)
    system_prompt: str = ""

    # Tool configuration
    tools: list[str] = Field(default_factory=_empty_str_list)
    tool_allowlist: list[str] | None = None

    # MCP configuration
    mcp_servers: list[str] | str = "auto"
    mcp_server_refs: list[MCPServerRef] = Field(default_factory=_empty_mcp_refs)

    # Permissions
    permissions: PermissionConfig = Field(default_factory=PermissionConfig)

    # Hooks (per-agent)
    hooks: list[HookDefinition] = Field(default_factory=_empty_hook_list)

    # Skills
    skills: list[SkillManifest] = Field(default_factory=_empty_skill_list)

    # Instructions
    instructions: list[InstructionManifest] = Field(
        default_factory=_empty_instruction_list,
    )

    # Delegation
    can_delegate: bool = False
    delegate_allowlist: list[str] = Field(default_factory=_empty_str_list)
    max_delegation_depth: int = 3

    # Agent type / supervisor fields
    agent_type: str = "loop"
    max_turns: int = 25
    tags: list[str] = Field(default_factory=_empty_str_list)

    # Source tracking
    source_path: Path | None = None

    model_config = {"arbitrary_types_allowed": True}


def normalize_frontmatter_key(key: str) -> str:
    """Normalise a frontmatter YAML key to a Python-friendly form.

    ``mcp-servers`` → ``mcp_servers``, etc.
    """
    return key.replace("-", "_").lower()


def agent_manifest_from_frontmatter(
    metadata: dict[str, Any],
    body: str,
    *,
    source_path: Path | None = None,
) -> AgentManifest:
    """Build an :class:`AgentManifest` from parsed frontmatter + body.

    Keys are normalised (hyphens → underscores) before mapping.  Unknown
    keys are silently ignored.
    """
    normalised: dict[str, Any] = {
        normalize_frontmatter_key(k): v for k, v in metadata.items()
    }

    # Map well-known fields
    kwargs: dict[str, Any] = {
        "system_prompt": body.strip(),
        "source_path": source_path,
    }

    _DIRECT_FIELDS = {
        "name", "description", "model", "tools", "tool_allowlist",
        "mcp_servers", "can_delegate", "delegate_allowlist",
        "max_delegation_depth", "agent_type", "max_turns", "tags",
    }
    for field_name in _DIRECT_FIELDS:
        if field_name in normalised:
            kwargs[field_name] = normalised[field_name]

    # Permissions
    raw_perms: Any = normalised.get("permissions")
    if isinstance(raw_perms, dict):
        perms_dict = cast("dict[str, Any]", raw_perms)
        p_allow: list[str] = list(perms_dict.get("allow") or [])
        p_deny: list[str] = list(perms_dict.get("deny") or [])
        kwargs["permissions"] = PermissionConfig(allow=p_allow, deny=p_deny)

    # Hooks (inline)
    raw_hooks: Any = normalised.get("hooks")
    if isinstance(raw_hooks, list):
        hook_defs: list[HookDefinition] = []
        for h_item in cast("list[Any]", raw_hooks):
            if isinstance(h_item, dict):
                hook_defs.append(HookDefinition(**cast("dict[str, Any]", h_item)))
        kwargs["hooks"] = hook_defs

    return AgentManifest(**kwargs)
