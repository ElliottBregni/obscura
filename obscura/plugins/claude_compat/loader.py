"""obscura.plugins.claude_compat.loader — Discover and load Claude Code plugins.

The :class:`ClaudePluginLoader` is the main entry point. It discovers
Claude Code plugin directories, adapts their manifests to Obscura's
:class:`PluginSpec`, and loads each component type into the right
Obscura subsystem:

  - Skills → registered as slash commands
  - Agents → registered as Obscura agent definitions
  - MCP servers → registered via Obscura's MCP infrastructure
  - Hooks → registered as supervisor hooks (shell commands)
  - bin/ → prepended to ``$PATH``

This module never modifies the native Obscura plugin pipeline. Claude
Code plugins are loaded as an *additional* source alongside builtins,
local, and user plugins.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from obscura.plugins.claude_compat.manifest_adapter import (
    CLAUDE_NS,
    adapt_claude_manifest,
    is_claude_plugin,
)
from obscura.plugins.claude_compat.skill_loader import (
    SkillCommand,
    load_skills_as_commands,
)
from obscura.plugins.claude_compat.variables import (
    get_plugin_data_dir,
    substitute_variables,
)

if TYPE_CHECKING:
    from obscura.plugins.broker import ToolBroker
    from obscura.plugins.models import PluginSpec

logger = logging.getLogger(__name__)

# Default search paths for Claude Code plugins.
_CLAUDE_PLUGIN_DIRS: list[Path] = [
    Path.home() / ".claude" / "plugins" / "cache",  # installed via Claude Code
    Path.home() / ".obscura" / "plugins" / "claude",  # installed via Obscura
]


class ClaudePluginLoader:
    """Discover and load Claude Code plugins into Obscura.

    Usage::

        loader = ClaudePluginLoader()
        results = loader.discover_and_load(broker)
        # results.skills → dict of registered slash commands
        # results.agents → list of agent definition paths
        # results.mcp_servers → dict of MCP server configs
        # results.loaded_specs → list of PluginSpec
    """

    def __init__(
        self,
        search_dirs: list[Path] | None = None,
        *,
        user_configs: dict[str, dict[str, str]] | None = None,
    ) -> None:
        self._search_dirs = search_dirs or list(_CLAUDE_PLUGIN_DIRS)
        self._user_configs = user_configs or {}

    # -- Discovery -------------------------------------------------------------

    def discover(self) -> list[PluginSpec]:
        """Find all Claude Code plugins in search directories.

        Also checks the current working directory and ``.claude/plugins/``.
        """
        specs: list[PluginSpec] = []
        seen_ids: set[str] = set()

        # Add project-level and CWD paths.
        extra_dirs = [
            Path.cwd() / ".claude" / "plugins",
            Path.cwd(),  # for --plugin-dir style
        ]

        for search_dir in self._search_dirs + extra_dirs:
            if not search_dir.is_dir():
                continue
            for candidate in self._walk_plugin_dirs(search_dir):
                spec = adapt_claude_manifest(candidate)
                if spec and spec.id not in seen_ids:
                    specs.append(spec)
                    seen_ids.add(spec.id)

        logger.info("Discovered %d Claude Code plugins", len(specs))
        return specs

    def _walk_plugin_dirs(self, root: Path) -> list[Path]:
        """Walk *root* looking for directories with ``.claude-plugin/plugin.json``.

        Handles both flat layout (root contains plugins directly) and
        nested layout (marketplace/plugin/version/).
        """
        candidates: list[Path] = []

        if is_claude_plugin(root):
            candidates.append(root)
            return candidates

        try:
            for child in sorted(root.iterdir()):
                if not child.is_dir():
                    continue
                if is_claude_plugin(child):
                    candidates.append(child)
                    continue
                # Nested: marketplace/plugin/version/ structure.
                for grandchild in sorted(child.iterdir()):
                    if not grandchild.is_dir():
                        continue
                    if is_claude_plugin(grandchild):
                        candidates.append(grandchild)
                        continue
                    # One more level: marketplace/plugin/version/
                    for great in sorted(grandchild.iterdir()):
                        if great.is_dir() and is_claude_plugin(great):
                            candidates.append(great)
        except PermissionError:
            logger.debug("Permission denied scanning %s", root)

        return candidates

    # -- Loading ---------------------------------------------------------------

    def discover_and_load(
        self,
        broker: ToolBroker | None = None,
    ) -> ClaudePluginLoadResult:
        """Discover all Claude Code plugins and load their components.

        Parameters
        ----------
        broker:
            Optional Obscura ToolBroker. If provided, MCP tool stubs are
            registered. Pass ``None`` for discovery-only mode.

        Returns
        -------
        ClaudePluginLoadResult with loaded components.
        """
        specs = self.discover()
        result = ClaudePluginLoadResult()

        for spec in specs:
            try:
                self._load_plugin(spec, broker, result)
                result.loaded_specs.append(spec)
            except Exception:
                logger.warning(
                    "Failed to load Claude Code plugin %s", spec.id, exc_info=True
                )
                result.errors.append(spec.id)

        logger.info(
            "Loaded %d Claude Code plugins (%d skills, %d agents, %d MCP servers, %d errors)",
            len(result.loaded_specs),
            len(result.skills),
            len(result.agents),
            len(result.mcp_servers),
            len(result.errors),
        )
        return result

    def _load_plugin(
        self,
        spec: PluginSpec,
        broker: ToolBroker | None,
        result: ClaudePluginLoadResult,
    ) -> None:
        """Load all components from a single Claude Code plugin."""
        plugin_dir = spec.source_dir
        if plugin_dir is None:
            return

        plugin_name = spec.name
        plugin_data = get_plugin_data_dir(spec.id)
        user_config = self._user_configs.get(spec.id, {})

        # Check for runtime override: load as native Python instead of MCP.
        try:
            from obscura.plugins.runtime_adapter import (
                RUNTIME_NATIVE,
                get_runtime_override,
                load_native_handlers_from_plugin,
            )

            override = get_runtime_override(spec.id)
            if override == RUNTIME_NATIVE:
                logger.info("Runtime override: loading %s as native Python", spec.id)
                handlers = load_native_handlers_from_plugin(plugin_dir)
                if broker and handlers:
                    from obscura.core.types import ToolSpec as ObscuraToolSpec

                    for tool_name, handler in handlers.items():
                        scoped = f"{CLAUDE_NS}:{plugin_name}:{tool_name}"
                        doc = getattr(handler, "__doc__", "") or ""
                        broker.register_tool_spec(
                            ObscuraToolSpec(
                                name=scoped,
                                description=doc.strip().split("\n")[0]
                                or f"Tool from {plugin_name}",
                                parameters={},
                                handler=handler,
                            )
                        )
                    logger.info(
                        "Registered %d native handlers from %s", len(handlers), spec.id
                    )
                # Still load skills/agents/hooks (non-tool components).
        except Exception:
            logger.debug("Runtime adapter not available", exc_info=True)
            override = None

        # 1. Skills → slash commands.
        skills = load_skills_as_commands(
            plugin_dir,
            plugin_name,
            plugin_data=plugin_data,
            user_config=user_config,
        )
        result.skills.update(skills)

        # 2. Agents → agent definitions.
        agents = self._load_agents(plugin_dir, plugin_name)
        result.agents.extend(agents)

        # 3. MCP servers — load configs and register with Obscura's MCP system.
        #    Skip if runtime was overridden to native (tools already loaded above).
        if override != RUNTIME_NATIVE:
            mcp = self._load_mcp_servers(
                plugin_dir, plugin_name, plugin_data, user_config
            )
            result.mcp_servers.update(mcp)
            if mcp:
                self._register_mcp_servers(mcp, spec.id)

        # 4. Hooks.
        hooks = self._load_hooks(plugin_dir, plugin_name, plugin_data, user_config)
        result.hooks.extend(hooks)

        # 5. bin/ → PATH extension.
        bin_dir = plugin_dir / "bin"
        if bin_dir.is_dir():
            current_path = os.environ.get("PATH", "")
            if str(bin_dir) not in current_path:
                os.environ["PATH"] = f"{bin_dir}:{current_path}"
                result.path_additions.append(bin_dir)

    # -- MCP bridge ------------------------------------------------------------

    def _register_mcp_servers(
        self,
        mcp_configs: dict[str, dict[str, Any]],
        plugin_id: str,
    ) -> None:
        """Write Claude Code MCP server configs into Obscura's MCP config dir.

        This makes them discoverable by Obscura's existing MCP auto-discovery
        and ``MCPToolProvider`` pipeline — no stubs needed, real MCP tools.
        """
        mcp_dir = Path.home() / ".obscura" / "mcp"
        mcp_dir.mkdir(parents=True, exist_ok=True)

        # Write a per-plugin MCP config file that Obscura's discovery will pick up.
        sanitized_id = plugin_id.replace(":", "_").replace("/", "_")
        config_path = mcp_dir / f"claude_plugin_{sanitized_id}.json"

        # Convert to Obscura's MCP config format.
        obscura_servers: dict[str, Any] = {}
        for scoped_name, config in mcp_configs.items():
            server_entry: dict[str, Any] = {}

            # Determine transport.
            if "url" in config:
                server_entry["transport"] = "sse"
                server_entry["url"] = config["url"]
            else:
                server_entry["command"] = config.get("command", "")
                server_entry["args"] = config.get("args", [])

            if "env" in config:
                server_entry["env"] = config["env"]

            server_entry["description"] = f"From Claude Code plugin {plugin_id}"
            obscura_servers[scoped_name] = server_entry

        if obscura_servers:
            import json

            payload = {"mcpServers": obscura_servers}
            config_path.write_text(
                json.dumps(payload, indent=2) + "\n", encoding="utf-8"
            )
            logger.info(
                "Registered %d MCP servers from %s → %s",
                len(obscura_servers),
                plugin_id,
                config_path,
            )

    # -- Component loaders -----------------------------------------------------

    def _load_agents(self, plugin_dir: Path, plugin_name: str) -> list[dict[str, Any]]:
        """Load agent definitions from ``agents/`` directory."""
        agents_dir = plugin_dir / "agents"
        if not agents_dir.is_dir():
            return []

        agents: list[dict[str, Any]] = []
        for md_file in sorted(agents_dir.rglob("*.md")):
            try:
                raw = md_file.read_text(encoding="utf-8")
                # Parse frontmatter.
                from obscura.plugins.claude_compat.skill_loader import (
                    _split_frontmatter,
                )

                import yaml

                fm_str, body = _split_frontmatter(raw)
                meta = yaml.safe_load(fm_str) if fm_str else {}
                if not isinstance(meta, dict):
                    meta = {}

                rel = md_file.relative_to(agents_dir)
                slug = ":".join(rel.with_suffix("").parts)
                agent_id = f"{CLAUDE_NS}:{plugin_name}:{slug}"

                agents.append(
                    {
                        "id": agent_id,
                        "name": meta.get("name", slug),
                        "description": meta.get("description", ""),
                        "model": meta.get("model", ""),
                        "system_prompt": body.strip(),
                        "max_turns": meta.get("maxTurns", 10),
                        "source": str(md_file),
                        "source_type": "claude_plugin",
                    }
                )
            except Exception:
                logger.debug("Could not parse agent %s", md_file, exc_info=True)

        return agents

    def _load_mcp_servers(
        self,
        plugin_dir: Path,
        plugin_name: str,
        plugin_data: Path,
        user_config: dict[str, str],
    ) -> dict[str, dict[str, Any]]:
        """Load MCP server configs from ``.mcp.json`` or manifest."""
        servers: dict[str, dict[str, Any]] = {}

        # Try .mcp.json in plugin root.
        mcp_path = plugin_dir / ".mcp.json"
        if mcp_path.exists():
            try:
                data = json.loads(mcp_path.read_text(encoding="utf-8"))
                raw_servers = data.get("mcpServers", data)
                if isinstance(raw_servers, dict):
                    for server_name, config in raw_servers.items():
                        if not isinstance(config, dict):
                            continue
                        scoped_name = f"{CLAUDE_NS}:{plugin_name}:{server_name}"
                        # Substitute variables in command and args.
                        config = self._substitute_mcp_config(
                            config, plugin_dir, plugin_data, user_config
                        )
                        servers[scoped_name] = config
            except Exception:
                logger.debug("Could not parse %s", mcp_path, exc_info=True)

        # Also check manifest mcpServers (inline configs).
        manifest_path = plugin_dir / ".claude-plugin" / "plugin.json"
        if manifest_path.exists():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                mcp_servers = manifest.get("mcpServers")
                if isinstance(mcp_servers, dict):
                    for server_name, config in mcp_servers.items():
                        if isinstance(config, str):
                            # Path reference — already handled above if it's .mcp.json.
                            continue
                        if isinstance(config, dict):
                            scoped_name = f"{CLAUDE_NS}:{plugin_name}:{server_name}"
                            if scoped_name not in servers:
                                config = self._substitute_mcp_config(
                                    config, plugin_dir, plugin_data, user_config
                                )
                                servers[scoped_name] = config
            except Exception:
                pass

        return servers

    def _substitute_mcp_config(
        self,
        config: dict[str, Any],
        plugin_dir: Path,
        plugin_data: Path,
        user_config: dict[str, str],
    ) -> dict[str, Any]:
        """Expand Claude Code variables in an MCP server config dict."""
        result: dict[str, Any] = {}
        for key, val in config.items():
            if isinstance(val, str):
                result[key] = substitute_variables(
                    val,
                    plugin_root=plugin_dir,
                    plugin_data=plugin_data,
                    user_config=user_config,
                )
            elif isinstance(val, list):
                result[key] = [
                    substitute_variables(
                        v,
                        plugin_root=plugin_dir,
                        plugin_data=plugin_data,
                        user_config=user_config,
                    )
                    if isinstance(v, str)
                    else v
                    for v in val
                ]
            elif isinstance(val, dict):
                result[key] = {
                    k: substitute_variables(
                        v,
                        plugin_root=plugin_dir,
                        plugin_data=plugin_data,
                        user_config=user_config,
                    )
                    if isinstance(v, str)
                    else v
                    for k, v in val.items()
                }
            else:
                result[key] = val
        return result

    def _load_hooks(
        self,
        plugin_dir: Path,
        plugin_name: str,
        plugin_data: Path,
        user_config: dict[str, str],
    ) -> list[dict[str, Any]]:
        """Load hook definitions from ``hooks/hooks.json``."""
        hooks_path = plugin_dir / "hooks" / "hooks.json"
        if not hooks_path.exists():
            return []

        try:
            data = json.loads(hooks_path.read_text(encoding="utf-8"))
        except Exception:
            logger.debug("Could not parse %s", hooks_path, exc_info=True)
            return []

        hooks_data = data.get("hooks", data)
        if not isinstance(hooks_data, dict):
            return []

        loaded: list[dict[str, Any]] = []
        for event_name, hook_entries in hooks_data.items():
            if not isinstance(hook_entries, list):
                continue
            for entry in hook_entries:
                if not isinstance(entry, dict):
                    continue
                # Substitute variables in hook commands.
                hook_list = entry.get("hooks", [])
                for hook in hook_list:
                    if isinstance(hook, dict) and "command" in hook:
                        hook["command"] = substitute_variables(
                            hook["command"],
                            plugin_root=plugin_dir,
                            plugin_data=plugin_data,
                            user_config=user_config,
                        )
                loaded.append(
                    {
                        "event": event_name,
                        "matcher": entry.get("matcher", ""),
                        "hooks": hook_list,
                        "source_plugin": f"{CLAUDE_NS}:{plugin_name}",
                    }
                )

        return loaded


class ClaudePluginLoadResult:
    """Aggregate result from loading Claude Code plugins."""

    def __init__(self) -> None:
        self.loaded_specs: list[PluginSpec] = []
        self.skills: dict[str, SkillCommand] = {}
        self.agents: list[dict[str, Any]] = []
        self.mcp_servers: dict[str, dict[str, Any]] = {}
        self.hooks: list[dict[str, Any]] = []
        self.path_additions: list[Path] = []
        self.errors: list[str] = []

    @property
    def summary(self) -> str:
        parts = []
        if self.loaded_specs:
            parts.append(f"{len(self.loaded_specs)} plugins")
        if self.skills:
            parts.append(f"{len(self.skills)} skills")
        if self.agents:
            parts.append(f"{len(self.agents)} agents")
        if self.mcp_servers:
            parts.append(f"{len(self.mcp_servers)} MCP servers")
        if self.hooks:
            parts.append(f"{len(self.hooks)} hooks")
        if self.errors:
            parts.append(f"{len(self.errors)} errors")
        return ", ".join(parts) if parts else "no plugins found"
