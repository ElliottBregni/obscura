"""obscura.manifest.loader — Load agent manifests from markdown + YAML frontmatter.

Reads ``*.agent.md`` files, skill directories, instruction files,
hooks.json, and settings.json to build :class:`AgentManifest` objects.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, cast

from obscura.core.frontmatter import parse_frontmatter_file
from obscura.manifest.models import (
    AgentManifest,
    HookDefinition,
    InstructionManifest,
    MCPServerRef,
    PermissionConfig,
    SkillManifest,
    agent_manifest_from_frontmatter,
)

logger = logging.getLogger(__name__)


class ManifestLoader:
    """Load agent manifests from markdown files with YAML frontmatter.

    Supports:

    - Single-file: ``*.agent.md`` with frontmatter + markdown body.
    - Skill files: ``SKILL.md`` or ``*.md`` inside a skills directory.
    - Instruction files: ``*.instructions.md`` with ``applyTo`` frontmatter.
    - Hooks: ``hooks.json`` with lifecycle hook definitions.
    - Permissions: ``settings.json`` with allow/deny patterns.
    - MCP configs: ``servers.json`` or ``.mcp.json``.
    """

    def __init__(self, base_dir: Path | None = None) -> None:
        self._base_dir = base_dir or Path.cwd()

    def _resolve(self, path: Path) -> Path:
        """Resolve relative paths against base_dir."""
        if path.is_absolute():
            return path
        return self._base_dir / path

    # ----- Agent manifests -----

    def load_agent_manifest(self, path: Path) -> AgentManifest:
        """Load a single agent manifest from an ``*.agent.md`` file."""
        resolved = self._resolve(path)
        result = parse_frontmatter_file(resolved)
        return agent_manifest_from_frontmatter(
            result.metadata, result.body, source_path=resolved,
        )

    def load_agent_manifests(self, directory: Path) -> list[AgentManifest]:
        """Scan *directory* for ``*.agent.md`` files and load each."""
        resolved = self._resolve(directory)
        if not resolved.is_dir():
            return []
        manifests: list[AgentManifest] = []
        for md_file in sorted(resolved.glob("*.agent.md")):
            try:
                manifests.append(self.load_agent_manifest(md_file))
            except Exception:
                logger.warning("Failed to load agent manifest %s", md_file, exc_info=True)
        return manifests

    # ----- Skills -----

    def load_skill_manifest(self, path: Path) -> SkillManifest:
        """Load a single skill manifest from a markdown file with frontmatter."""
        resolved = self._resolve(path)
        result = parse_frontmatter_file(resolved)
        meta = result.metadata
        return SkillManifest(
            name=str(meta.get("name", resolved.stem)),
            description=str(meta.get("description", "")),
            user_invocable=bool(meta.get("user-invocable", meta.get("user_invocable", True))),
            allowed_tools=_str_list(meta.get("allowed-tools", meta.get("allowed_tools"))),
            body=result.body.strip(),
            source_path=resolved,
        )

    def load_skills_from_directory(self, directory: Path) -> list[SkillManifest]:
        """Load all ``*.md`` skill files from *directory* (recursive)."""
        resolved = self._resolve(directory)
        if not resolved.is_dir():
            return []
        skills: list[SkillManifest] = []
        for md_file in sorted(resolved.rglob("*.md")):
            if not md_file.is_file():
                continue
            try:
                skills.append(self.load_skill_manifest(md_file))
            except Exception:
                logger.warning("Failed to load skill %s", md_file, exc_info=True)
        return skills

    # ----- Instructions -----

    def load_instruction_manifest(self, path: Path) -> InstructionManifest:
        """Load a single instruction file, parsing ``applyTo`` from frontmatter."""
        resolved = self._resolve(path)
        result = parse_frontmatter_file(resolved)
        meta = result.metadata
        raw_apply: Any = meta.get("applyTo", meta.get("apply_to"))
        apply_to: list[str] = []
        if isinstance(raw_apply, str):
            apply_to = [p.strip() for p in raw_apply.split(",") if p.strip()]
        elif isinstance(raw_apply, list):
            apply_to = [str(p) for p in cast("list[Any]", raw_apply)]
        return InstructionManifest(
            apply_to=apply_to,
            body=result.body.strip(),
            source_path=resolved,
        )

    def load_instructions_from_directory(
        self, directory: Path,
    ) -> list[InstructionManifest]:
        """Load all ``*.instructions.md`` and ``*.md`` from *directory*."""
        resolved = self._resolve(directory)
        if not resolved.is_dir():
            return []
        instructions: list[InstructionManifest] = []
        for md_file in sorted(resolved.rglob("*.md")):
            if not md_file.is_file():
                continue
            try:
                instructions.append(self.load_instruction_manifest(md_file))
            except Exception:
                logger.warning("Failed to load instruction %s", md_file, exc_info=True)
        return instructions

    # ----- Hooks -----

    def load_hooks_from_json(self, path: Path) -> list[HookDefinition]:
        """Load hooks from a ``hooks.json`` file.

        Flattens the nested structure::

            {
              "hooks": {
                "preToolUse": [{"type": "command", "command": "..."}],
                "postToolUse": [...]
              }
            }

        into a flat list of :class:`HookDefinition`.
        """
        resolved = self._resolve(path)
        if not resolved.is_file():
            return []
        try:
            raw: Any = json.loads(resolved.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("Failed to parse hooks JSON %s", resolved, exc_info=True)
            return []

        hooks_section: Any = raw
        if isinstance(raw, dict):
            raw_top = cast("dict[str, Any]", raw)
            if "hooks" in raw_top:
                hooks_section = raw_top["hooks"]

        definitions: list[HookDefinition] = []
        if not isinstance(hooks_section, dict):
            return definitions

        hooks_dict = cast("dict[str, Any]", hooks_section)
        for event_name, entries in hooks_dict.items():
            if not isinstance(entries, list):
                continue
            for entry in cast("list[Any]", entries):
                if isinstance(entry, dict):
                    entry_dict = cast("dict[str, Any]", entry)
                    definitions.append(HookDefinition(
                        event=str(event_name),
                        type=str(entry_dict.get("type", "command")),
                        bash=str(entry_dict.get("command", entry_dict.get("bash", ""))),
                        module=str(entry_dict.get("module", "")),
                        timeout_sec=int(entry_dict.get("timeout", entry_dict.get("timeout_sec", 10))),
                        comment=str(entry_dict.get("comment", "")),
                    ))
        return definitions

    # ----- Permissions -----

    def load_permissions(self, path: Path) -> PermissionConfig:
        """Load permissions from a ``settings.json`` file."""
        resolved = self._resolve(path)
        if not resolved.is_file():
            return PermissionConfig()
        try:
            raw: Any = json.loads(resolved.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("Failed to parse settings %s", resolved, exc_info=True)
            return PermissionConfig()

        if not isinstance(raw, dict):
            return PermissionConfig()

        raw_dict = cast("dict[str, Any]", raw)
        perms: Any = raw_dict.get("permissions", raw_dict)
        if not isinstance(perms, dict):
            return PermissionConfig()

        perms_dict = cast("dict[str, Any]", perms)
        allow: list[str] = _str_list(perms_dict.get("allow"))
        deny: list[str] = _str_list(perms_dict.get("deny"))
        return PermissionConfig(allow=allow, deny=deny)

    # ----- MCP Server Refs -----

    def load_mcp_server_refs(self, path: Path) -> list[MCPServerRef]:
        """Load MCP server references from a ``servers.json`` or ``.mcp.json``."""
        resolved = self._resolve(path)
        if not resolved.is_file():
            return []
        try:
            raw: Any = json.loads(resolved.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("Failed to parse MCP config %s", resolved, exc_info=True)
            return []

        # Support both {"mcpServers": {...}} and {"servers": [...]} formats
        servers_raw: Any = raw
        if isinstance(raw, dict):
            raw_dict = cast("dict[str, Any]", raw)
            if "mcpServers" in raw_dict:
                servers_raw = raw_dict["mcpServers"]
            elif "servers" in raw_dict:
                servers_raw = raw_dict["servers"]

        refs: list[MCPServerRef] = []

        if isinstance(servers_raw, dict):
            # {"serverName": {"command": ..., "args": [...]}}
            srv_dict = cast("dict[str, Any]", servers_raw)
            for name, config in srv_dict.items():
                if isinstance(config, dict):
                    cfg = cast("dict[str, Any]", config)
                    refs.append(MCPServerRef(
                        name=str(name),
                        transport=str(cfg.get("transport", "stdio")),
                        command=str(cfg.get("command", "")),
                        args=_str_list(cfg.get("args")),
                        env=_str_dict(cfg.get("env")),
                        url=str(cfg.get("url", "")),
                    ))
        elif isinstance(servers_raw, list):
            for item in cast("list[Any]", servers_raw):
                if isinstance(item, dict):
                    cfg = cast("dict[str, Any]", item)
                    refs.append(MCPServerRef(
                        name=str(cfg.get("name", "")),
                        transport=str(cfg.get("transport", "stdio")),
                        command=str(cfg.get("command", "")),
                        args=_str_list(cfg.get("args")),
                        env=_str_dict(cfg.get("env")),
                        url=str(cfg.get("url", "")),
                    ))

        return refs


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _str_list(val: Any) -> list[str]:
    """Coerce a value to ``list[str]``, returning [] for None/non-list."""
    if val is None:
        return []
    if isinstance(val, list):
        return [str(item) for item in cast("list[Any]", val)]
    return []


def _str_dict(val: Any) -> dict[str, str]:
    """Coerce a value to ``dict[str, str]``, returning {} for None/non-dict."""
    if val is None:
        return {}
    if isinstance(val, dict):
        d = cast("dict[str, Any]", val)
        return {str(k): str(d[k]) for k in d}
    return {}
