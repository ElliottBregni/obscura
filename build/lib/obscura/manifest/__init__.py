"""obscura.manifest — Declarative agent manifest loading from markdown + YAML frontmatter."""

from __future__ import annotations

from obscura.manifest.models import (
    AgentManifest,
    HookDefinition,
    InstructionManifest,
    MCPServerRef,
    PermissionConfig,
    SkillManifest,
)

__all__ = [
    "AgentManifest",
    "HookDefinition",
    "InstructionManifest",
    "MCPServerRef",
    "PermissionConfig",
    "SkillManifest",
]
