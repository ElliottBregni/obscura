"""obscura.plugins.claude_compat.manifest_adapter — Convert plugin.json to Obscura PluginSpec.

Reads a Claude Code ``plugin.json`` manifest and produces an Obscura
:class:`PluginSpec` that the native loader/broker can consume. The
resulting spec uses ``source_type="claude"`` and
``runtime_type="claude"`` so native code can distinguish it.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, cast

from obscura.plugins.models import (
    CapabilitySpec,
    ConfigRequirement,
    InstructionSpec,
    PluginSpec,
    ToolContribution,
)

logger = logging.getLogger(__name__)

# Namespace prefix for all Claude Code plugins inside Obscura.
CLAUDE_NS = "claude"


def _as_str(value: Any, default: str = "") -> str:
    """Coerce *value* to ``str`` (returning *default* when not a string)."""
    return value if isinstance(value, str) else default


def _as_bool(value: Any, default: bool = False) -> bool:
    """Coerce *value* to ``bool`` (returning *default* when not a bool)."""
    return value if isinstance(value, bool) else default


def adapt_claude_manifest(
    plugin_dir: Path,
    *,
    marketplace: str = "local",
) -> PluginSpec | None:
    """Parse ``plugin.json`` and convert to an Obscura :class:`PluginSpec`.

    Parameters
    ----------
    plugin_dir:
        Root directory of the Claude Code plugin (parent of
        ``.claude-plugin/``).
    marketplace:
        Marketplace name this plugin came from (for ID namespacing).

    Returns
    -------
    PluginSpec or None if the manifest can't be parsed.
    """
    manifest_path = plugin_dir / ".claude-plugin" / "plugin.json"
    if not manifest_path.exists():
        logger.debug("No .claude-plugin/plugin.json in %s", plugin_dir)
        return None

    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("Failed to parse %s", manifest_path, exc_info=True)
        return None

    if not isinstance(raw, dict):
        logger.warning("plugin.json is not a JSON object: %s", manifest_path)
        return None

    data: dict[str, Any] = cast(dict[str, Any], raw)

    name = _as_str(data.get("name", plugin_dir.name), plugin_dir.name)
    plugin_id = f"{CLAUDE_NS}:{name}"
    version = _as_str(data.get("version", "0.0.0"), "0.0.0")

    # -- Config requirements from userConfig -----------------------------------
    user_config_raw = data.get("userConfig")
    user_config: dict[str, Any] | None = (
        cast(dict[str, Any], user_config_raw)
        if isinstance(user_config_raw, dict)
        else None
    )
    config_reqs = _parse_user_config(user_config)

    # -- Capabilities: one catch-all capability for the plugin -----------------
    cap_id = f"{CLAUDE_NS}.{name.replace('-', '_')}"
    capabilities: tuple[CapabilitySpec, ...] = (
        CapabilitySpec(
            id=cap_id,
            version=version,
            description=_as_str(
                data.get("description", f"Claude Code plugin: {name}"),
                f"Claude Code plugin: {name}",
            ),
            tools=(),  # filled after tool discovery
            requires_approval=False,
            default_grant=True,
        ),
    )

    # -- Discover tools from MCP server declarations ---------------------------
    tools = _extract_mcp_tool_stubs(data, plugin_id)

    # -- Instructions from plugin description ----------------------------------
    instructions: tuple[InstructionSpec, ...] = ()
    desc_raw = data.get("description")
    desc = _as_str(desc_raw)
    if desc:
        instructions = (
            InstructionSpec(
                id=f"{plugin_id}:description",
                version=version,
                scope="global",
                content=desc,
                priority=80,
            ),
        )

    # -- Component paths (stored in source_dir for later loading) --------------
    # We don't parse skills/agents/hooks here — the ClaudePluginLoader does
    # that separately using the component loaders.

    return PluginSpec(
        id=plugin_id,
        name=name,
        version=version,
        source_type="claude",
        runtime_type="claude",
        trust_level="community",
        author=_extract_author(data),
        description=_as_str(data.get("description", "")),
        source_dir=plugin_dir,
        config_requirements=config_reqs,
        capabilities=capabilities,
        tools=tuple(tools),
        instructions=instructions,
    )


def is_claude_plugin(path: Path) -> bool:
    """Return True if *path* looks like a Claude Code plugin directory."""
    return (path / ".claude-plugin" / "plugin.json").exists()


# -- Internal helpers ---------------------------------------------------------


def _parse_user_config(
    user_config: dict[str, Any] | None,
) -> tuple[ConfigRequirement, ...]:
    """Convert Claude Code ``userConfig`` to Obscura config requirements."""
    if not user_config:
        return ()
    reqs: list[ConfigRequirement] = []
    for key, spec_raw in user_config.items():
        if not isinstance(spec_raw, dict):
            continue
        spec: dict[str, Any] = cast(dict[str, Any], spec_raw)
        # Map Claude types to Obscura types.
        cc_type = _as_str(spec.get("type", "string"), "string")
        obscura_type = {"string": "string", "number": "int", "boolean": "bool"}.get(
            cc_type, "string"
        )
        if spec.get("sensitive"):
            obscura_type = "secret"
        default_val = spec.get("default")
        reqs.append(
            ConfigRequirement(
                key=f"CLAUDE_PLUGIN_{key.upper()}",
                type=obscura_type,
                required=_as_bool(spec.get("required", False)),
                description=_as_str(
                    spec.get("description", spec.get("title", key)), key
                ),
                default=str(default_val) if default_val is not None else None,
            )
        )
    return tuple(reqs)


def _extract_author(data: dict[str, Any]) -> str:
    """Extract author string from manifest."""
    author = data.get("author")
    if isinstance(author, dict):
        author_dict: dict[str, Any] = cast(dict[str, Any], author)
        return _as_str(author_dict.get("name", ""))
    if isinstance(author, str):
        return author
    return ""


def _extract_mcp_tool_stubs(
    data: dict[str, Any], plugin_id: str
) -> list[ToolContribution]:
    """Create placeholder tool contributions for declared MCP servers.

    Real tools come from the MCP protocol at runtime — these stubs let
    the broker know the plugin contributes tools and allow capability
    gating to work.
    """
    tools: list[ToolContribution] = []
    mcp_servers = data.get("mcpServers")

    if isinstance(mcp_servers, dict):
        servers_dict: dict[Any, Any] = cast(dict[Any, Any], mcp_servers)
        for server_name in servers_dict:
            if isinstance(server_name, str):
                tools.append(
                    ToolContribution(
                        name=f"{plugin_id}:mcp:{server_name}",
                        description=f"MCP server '{server_name}' from Claude Code plugin",
                        handler_ref="",  # resolved at MCP connect time
                        capability=f"{CLAUDE_NS}.{plugin_id.split(':')[-1].replace('-', '_')}",
                        side_effects="write",
                    )
                )
    return tools
