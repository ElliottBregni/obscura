"""obscura.plugins.runtime_adapter — Bidirectional runtime adapter for plugins.

Allows any plugin to run in either mode regardless of its native format:

  - **Native → MCP**: Wrap Obscura Python tool handlers in a FastMCP server
    process, exposable over stdio or SSE.
  - **MCP → Native**: Import a Python module from a Claude Code plugin and
    register its functions directly as native tool handlers (no MCP overhead).

Configuration is per-plugin via ``config.toml``::

    [plugins.runtime_overrides]
    # Run a native plugin as an MCP server instead.
    gws = "mcp"

    # Run a Claude Code plugin's Python module as native handlers.
    "claude:my-plugin" = "native"

Or via environment variable::

    OBSCURA_PLUGIN_RUNTIME_<ID>=mcp|native

Usage::

    from obscura.plugins.runtime_adapter import (
        get_runtime_override,
        wrap_tools_as_mcp_server,
        load_native_handlers_from_plugin,
    )
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from obscura.core.types import ToolSpec

logger = logging.getLogger(__name__)

# Valid runtime modes.
RUNTIME_NATIVE = "native"
RUNTIME_MCP = "mcp"


def get_runtime_override(plugin_id: str) -> str | None:
    """Check if a plugin has a runtime override configured.

    Resolution order:
    1. ``OBSCURA_PLUGIN_RUNTIME_<ID>`` env var (uppercase, hyphens→underscores)
    2. ``[plugins.runtime_overrides]`` in config.toml
    3. None (use the plugin's default runtime)
    """
    # Env var check.
    env_key = (
        "OBSCURA_PLUGIN_RUNTIME_" + re.sub(r"[^A-Za-z0-9]", "_", plugin_id).upper()
    )
    env_val = os.environ.get(env_key, "").strip().lower()
    if env_val in (RUNTIME_NATIVE, RUNTIME_MCP):
        return env_val

    # Config.toml check.
    try:
        from obscura.core.config_io import try_load_config

        for config_path in (
            Path.cwd() / ".obscura" / "config.toml",
            Path.home() / ".obscura" / "config.toml",
        ):
            cfg = try_load_config(config_path)
            if cfg:
                overrides = cfg.get("plugins", {}).get("runtime_overrides", {})
                val = overrides.get(plugin_id, "").strip().lower()
                if val in (RUNTIME_NATIVE, RUNTIME_MCP):
                    return val
    except Exception:
        logger.debug("suppressed exception in get_runtime_override", exc_info=True)

    return None


# ---------------------------------------------------------------------------
# Native → MCP: Wrap Python tool handlers as a FastMCP server
# ---------------------------------------------------------------------------


def wrap_tools_as_mcp_server(
    tools: list[ToolSpec],
    *,
    server_name: str = "obscura-plugin",
    host: str = "127.0.0.1",  # noqa: ARG001
    port: int = 0,  # noqa: ARG001
) -> dict[str, Any]:
    """Generate an MCP server config that wraps Python tool handlers.

    Writes a thin FastMCP server script to a temp file and returns an
    ``MCPConnectionConfig``-compatible dict pointing at it.

    Parameters
    ----------
    tools:
        List of Obscura ToolSpec objects with callable handlers.
    server_name:
        Name for the MCP server.
    host:
        Bind address (only used for SSE transport).
    port:
        Port (0 = auto-assign, only for SSE).

    Returns
    -------
    Dict compatible with Obscura's MCP config format::

        {"command": "python", "args": ["/path/to/server.py"], "env": {}}
    """
    # Build the server script.
    tool_defs: list[str] = []
    handler_imports: list[str] = []

    for i, tool in enumerate(tools):
        handler = tool.handler
        if handler is None:
            continue

        # Resolve handler module path for import.
        mod = getattr(handler, "__module__", None)
        qual = getattr(
            handler, "__qualname__", getattr(handler, "__name__", f"tool_{i}")
        )
        func_name = qual.split(".")[-1]

        if mod and mod != "__main__":
            handler_imports.append(f"from {mod} import {func_name} as _handler_{i}")
        else:
            # Inline handler — skip (can't serialize closures).
            logger.debug("Skipping tool %s: handler not importable", tool.name)
            continue

        tool_defs.append(
            f'@mcp.tool(name="{tool.name}", description="""{tool.description}""")\n'
            f"async def tool_{i}(**kwargs):\n"
            f"    import asyncio, inspect\n"
            f"    result = _handler_{i}(**kwargs)\n"
            f"    if inspect.isawaitable(result): result = await result\n"
            f"    return str(result)\n"
        )

    if not tool_defs:
        logger.warning("No importable tool handlers for MCP server %s", server_name)
        return {}

    script = (
        "from __future__ import annotations\n"
        "from fastmcp import FastMCP\n" + "\n".join(handler_imports) + "\n\n"
        f'mcp = FastMCP(name="{server_name}")\n\n'
        + "\n".join(tool_defs)
        + '\nif __name__ == "__main__":\n'
        + '    mcp.run(transport="stdio")\n'
    )

    # Write to a persistent location.
    server_dir = Path.home() / ".obscura" / "plugins" / "mcp_wrappers"
    server_dir.mkdir(parents=True, exist_ok=True)
    sanitized = re.sub(r"[^\w-]", "_", server_name)
    script_path = server_dir / f"{sanitized}_mcp.py"
    script_path.write_text(script, encoding="utf-8")

    logger.info(
        "Generated MCP wrapper for %s (%d tools) → %s",
        server_name,
        len(tool_defs),
        script_path,
    )

    return {
        "command": sys.executable,
        "args": [str(script_path)],
        "env": {},
        "description": f"MCP wrapper for Obscura plugin '{server_name}'",
    }


def write_mcp_config_for_plugin(
    plugin_id: str,
    tools: list[ToolSpec],
) -> Path | None:
    """Generate an MCP server for a native plugin and write its config.

    Writes to ``~/.obscura/mcp/plugin_mcp_<id>.json`` so Obscura's
    MCP auto-discovery picks it up.

    Returns the config file path, or None if no tools could be wrapped.
    """
    server_config = wrap_tools_as_mcp_server(tools, server_name=plugin_id)
    if not server_config:
        return None

    mcp_dir = Path.home() / ".obscura" / "mcp"
    mcp_dir.mkdir(parents=True, exist_ok=True)

    sanitized = re.sub(r"[^\w-]", "_", plugin_id)
    config_path = mcp_dir / f"plugin_mcp_{sanitized}.json"

    scoped_name = f"plugin:{plugin_id}"
    payload = {"mcpServers": {scoped_name: server_config}}
    config_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    logger.info("MCP config for %s → %s", plugin_id, config_path)
    return config_path


# ---------------------------------------------------------------------------
# MCP → Native: Load Python handlers from a Claude Code plugin
# ---------------------------------------------------------------------------


def load_native_handlers_from_plugin(
    plugin_dir: Path,
    *,
    module_path: str = "",
    handler_map: dict[str, str] | None = None,
) -> dict[str, Callable[..., Any]]:
    """Import Python functions from a Claude Code plugin as native handlers.

    Parameters
    ----------
    plugin_dir:
        Root directory of the plugin.
    module_path:
        Dotted module path relative to plugin_dir (e.g., ``"tools.handlers"``).
        If empty, tries ``tools``, ``handlers``, ``src.tools``.
    handler_map:
        Optional ``{tool_name: function_name}`` mapping. If not provided,
        all public callables in the module are returned.

    Returns
    -------
    Dict of ``{tool_name: callable}`` for registration with the broker.
    """
    handlers: dict[str, Callable[..., Any]] = {}

    # Add plugin dir to sys.path temporarily.
    plugin_str = str(plugin_dir)
    added_to_path = False
    if plugin_str not in sys.path:
        sys.path.insert(0, plugin_str)
        added_to_path = True

    try:
        # Determine module to import.
        candidates = (
            [module_path]
            if module_path
            else ["tools", "handlers", "src.tools", "src.handlers"]
        )

        mod = None
        for candidate in candidates:
            if not candidate:
                continue
            try:
                mod = importlib.import_module(candidate)
                logger.debug("Imported module %s from %s", candidate, plugin_dir)
                break
            except ImportError:
                logger.debug(
                    "suppressed exception in load_native_handlers_from_plugin",
                    exc_info=True,
                )
                continue

        if mod is None:
            logger.debug("No importable handler module found in %s", plugin_dir)
            return handlers

        if handler_map:
            # Explicit mapping.
            for tool_name, func_name in handler_map.items():
                func = getattr(mod, func_name, None)
                if callable(func):
                    handlers[tool_name] = func
        else:
            # Auto-discover all public callables.
            for name in dir(mod):
                if name.startswith("_"):
                    continue
                obj = getattr(mod, name)
                if callable(obj) and not isinstance(obj, type):
                    handlers[name] = obj

    except Exception:
        logger.warning(
            "Failed to load native handlers from %s", plugin_dir, exc_info=True
        )

    finally:
        if added_to_path:
            with __import__("contextlib").suppress(ValueError):
                sys.path.remove(plugin_str)

    logger.info("Loaded %d native handlers from %s", len(handlers), plugin_dir)
    return handlers
