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
from typing import Any

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
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("Failed to parse %s", manifest_path, exc_info=True)
        return None

    if not isinstance(data, dict):
        logger.warning("plugin.json is not a JSON object: %s", manifest_path)
        return None

    name = data.get("name", plugin_dir.name)
    plugin_id = f"{CLAUDE_NS}:{name}"
    version = data.get("version", "0.0.0")

    # -- Config requirements from userConfig -----------------------------------
    config_reqs = _parse_user_config(data.get("userConfig"))

    # -- Capabilities: one catch-all capability for the plugin -----------------
    cap_id = f"{CLAUDE_NS}.{name.replace('-', '_')}"
    capabilities = (
        CapabilitySpec(
            id=cap_id,
            version=version,
            description=data.get("description", f"Claude Code plugin: {name}"),
            tools=(),  # filled after tool discovery
            requires_approval=False,
            default_grant=True,
        ),
    )

    # -- Discover tools from MCP server declarations ---------------------------
    tools = _extract_mcp_tool_stubs(data, plugin_id)

    # -- Instructions from plugin description ----------------------------------
    instructions = ()
    desc = data.get("description")
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
        description=data.get("description", ""),
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
    if not user_config or not isinstance(user_config, dict):
        return ()
    reqs: list[ConfigRequirement] = []
    for key, spec in user_config.items():
        if not isinstance(spec, dict):
            continue
        # Map Claude types to Obscura types.
        cc_type = spec.get("type", "string")
        obscura_type = {"string": "string", "number": "int", "boolean": "bool"}.get(
            cc_type, "string"
        )
        if spec.get("sensitive"):
            obscura_type = "secret"
        reqs.append(
            ConfigRequirement(
                key=f"CLAUDE_PLUGIN_{key.upper()}",
                type=obscura_type,
                required=spec.get("required", False),
                description=spec.get("description", spec.get("title", key)),
                default=str(spec.get("default", ""))
                if spec.get("default") is not None
                else None,
            )
        )
    return tuple(reqs)


def _extract_author(data: dict[str, Any]) -> str:
    """Extract author string from manifest."""
    author = data.get("author")
    if isinstance(author, dict):
        return author.get("name", "")
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
        for server_name in mcp_servers:
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
