"""obscura.manifest.lazy — Lazy loading proxy for agent manifests.

Wraps an :class:`AgentManifest` and defers expensive operations
(tool policy construction, hook registry compilation, MCP resolution,
system prompt composition) until first access.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Generic, TypeVar

from obscura.manifest.models import AgentManifest, InstructionManifest, SkillManifest

logger = logging.getLogger(__name__)

T = TypeVar("T")


class LazyField(Generic[T]):
    """Descriptor that computes its value on first ``.get()``, then caches it."""

    def __init__(self, factory: Callable[[], T]) -> None:
        self._factory = factory
        self._value: T | None = None
        self._resolved = False

    def get(self) -> T:
        if not self._resolved:
            self._value = self._factory()
            self._resolved = True
        assert self._value is not None or self._resolved
        return self._value  # type: ignore[return-value]

    def invalidate(self) -> None:
        """Force re-resolution on next access."""
        self._resolved = False
        self._value = None

    @property
    def is_resolved(self) -> bool:
        return self._resolved


class LazyManifestProxy:
    """Wraps an :class:`AgentManifest` and provides lazy resolution.

    Expensive operations (tool policy construction, hook compilation,
    MCP server resolution, system prompt composition) are deferred until
    the corresponding property is first accessed.
    """

    def __init__(self, manifest: AgentManifest) -> None:
        self._manifest = manifest
        self._tool_policy = LazyField(self._build_tool_policy)
        self._hook_registry = LazyField(self._build_hook_registry)
        self._mcp_configs = LazyField(self._resolve_mcp_configs)
        self._resolved_skills = LazyField(self._resolve_skills)
        self._resolved_instructions = LazyField(self._resolve_instructions)
        self._system_prompt = LazyField(self._build_system_prompt)

    @property
    def manifest(self) -> AgentManifest:
        return self._manifest

    @property
    def tool_policy(self) -> Any:
        """Lazily-built :class:`ToolPolicy` from manifest permissions."""
        return self._tool_policy.get()

    @property
    def hook_registry(self) -> Any:
        """Lazily-built :class:`HookRegistry` from manifest hook definitions."""
        return self._hook_registry.get()

    @property
    def mcp_configs(self) -> list[dict[str, Any]]:
        """Lazily-resolved MCP server configurations."""
        return self._mcp_configs.get()

    @property
    def skills(self) -> list[SkillManifest]:
        """Lazily-resolved skill manifests."""
        return self._resolved_skills.get()

    @property
    def instructions(self) -> list[InstructionManifest]:
        """Lazily-resolved instruction manifests."""
        return self._resolved_instructions.get()

    @property
    def system_prompt(self) -> str:
        """Composited system prompt from body + instructions + skills."""
        return self._system_prompt.get()

    def invalidate_all(self) -> None:
        """Force all lazy fields to re-resolve on next access."""
        self._tool_policy.invalidate()
        self._hook_registry.invalidate()
        self._mcp_configs.invalidate()
        self._resolved_skills.invalidate()
        self._resolved_instructions.invalidate()
        self._system_prompt.invalidate()

    # ----- Factory methods -----

    def _build_tool_policy(self) -> Any:
        """Build ToolPolicy from manifest permissions."""
        from obscura.tools.policy.models import ToolPolicy

        perms = self._manifest.permissions
        return ToolPolicy(
            name=self._manifest.name,
            allow_list=frozenset(perms.allow) if perms.allow else frozenset(),
            deny_list=frozenset(perms.deny) if perms.deny else frozenset(),
        )

    def _build_hook_registry(self) -> Any:
        """Build HookRegistry from manifest hook definitions."""
        from obscura.core.hooks import HookRegistry

        if not self._manifest.hooks:
            return HookRegistry()

        return HookRegistry.from_hook_definitions(self._manifest.hooks)

    def _resolve_mcp_configs(self) -> list[dict[str, Any]]:
        """Resolve MCP server refs to runtime-ready configs."""
        configs: list[dict[str, Any]] = []
        for ref in self._manifest.mcp_server_refs:
            config: dict[str, Any] = {
                "transport": ref.transport,
                "command": ref.command,
                "args": list(ref.args),
                "env": dict(ref.env),
            }
            if ref.url:
                config["url"] = ref.url
            configs.append(config)
        return configs

    def _resolve_skills(self) -> list[SkillManifest]:
        return list(self._manifest.skills)

    def _resolve_instructions(self) -> list[InstructionManifest]:
        return list(self._manifest.instructions)

    def _build_system_prompt(self) -> str:
        """Composite system prompt from body + instructions + skills."""
        parts: list[str] = []
        if self._manifest.system_prompt:
            parts.append(self._manifest.system_prompt)
        for instruction in self.instructions:
            if instruction.body:
                parts.append(instruction.body)
        for skill in self.skills:
            if skill.body:
                parts.append(f"## Skill: {skill.name}\n\n{skill.body}")
        return "\n\n".join(parts)
