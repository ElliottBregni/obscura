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
    """Instruction manifest for context-specific agent behavior."""
    
    apply_to: list[str] = Field(default_factory=_empty_str_list)
    body: str = ""
    
    model_config = {"arbitrary_types_allowed": True}


class MCPServerRef(BaseModel):
    """Reference to an MCP (Model Context Protocol) server configuration."""
    
    name: str
    transport: str = "stdio"
    command: str = ""
    args: list[str] = Field(default_factory=_empty_str_list)
    env: dict[str, str] = Field(default_factory=_empty_dict)
    
    model_config = {"arbitrary_types_allowed": True}


def _empty_instruction_list() -> list[InstructionManifest]:
    return []


def _empty_mcp_refs() -> list[MCPServerRef]:
    return []


class AgentManifest(BaseModel):
    """Complete parsed agent manifest from an ``*.agent.md`` file.
    """
    name: str
    description: str = ""
    provider: str = "copilot"
    model_id: str | None = None
    system_prompt: str = ""
    tools: list[str] = Field(default_factory=_empty_str_list)
    tool_allowlist: list[str] | None = None
    mcp_servers: list[MCPServerRef] = Field(default_factory=_empty_mcp_refs)
    permissions: PermissionConfig = Field(default_factory=PermissionConfig)
    hooks: list[HookDefinition] = Field(default_factory=list)

    skills_config: dict[str, Any] = Field(default_factory=dict)

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

    @property
    def model(self) -> str:
        """Deprecated: use provider instead."""
        import warnings
        warnings.warn(
            "AgentManifest.model is deprecated. Use .provider instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.provider


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
        "name", "description", "provider", "model_id", "tools", "tool_allowlist",
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

    # Skills loading config (lazy_load, filter)
    raw_skills: Any = normalised.get("skills")
    if isinstance(raw_skills, dict):
        kwargs["skills_config"] = cast("dict[str, Any]", raw_skills)

    # Backward compatibility: accept 'model' field and map to 'provider'
    if "model" in normalised and "provider" not in normalised:
        kwargs["provider"] = normalised["model"]

    return AgentManifest(**kwargs)
