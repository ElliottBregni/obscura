"""obscura.plugins.claude_compat — Adapter for Claude Code plugins.

Allows Claude Code plugins (directory + ``.claude-plugin/plugin.json``)
to be discovered, loaded, and executed inside Obscura without modifying
Obscura's native plugin system.

Components are mapped as follows:

  ========================  ================================
  Claude Code component     Obscura subsystem
  ========================  ================================
  skills/ (SKILL.md)        Slash commands (prompt injection)
  agents/ (*.md)            Agent definitions
  hooks/hooks.json          Supervisor hooks (shell commands)
  .mcp.json / mcpServers    MCP server registry
  bin/                      PATH extension for run_shell
  userConfig                Config requirements (env vars)
  ========================  ================================

All Claude Code plugins are namespaced under ``claude:<plugin-name>``
to avoid collisions with native Obscura plugins.

Usage::

    from obscura.plugins.claude_compat import ClaudePluginLoader

    loader = ClaudePluginLoader()
    specs = loader.discover()
    for spec in specs:
        loader.load(spec, broker)
"""

from __future__ import annotations

from obscura.plugins.claude_compat.loader import ClaudePluginLoader
from obscura.plugins.claude_compat.manifest_adapter import adapt_claude_manifest
from obscura.plugins.claude_compat.marketplace import MarketplaceResolver
from obscura.plugins.claude_compat.skill_loader import load_skills_as_commands
from obscura.plugins.claude_compat.variables import substitute_variables

__all__ = [
    "ClaudePluginLoader",
    "MarketplaceResolver",
    "adapt_claude_manifest",
    "load_skills_as_commands",
    "substitute_variables",
]
