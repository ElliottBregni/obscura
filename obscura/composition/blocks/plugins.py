"""obscura.composition.blocks.plugins — install builtin plugin tools.

This is the canonical plugin-loading path for every surface. After this
block runs, `session.registry` contains every plugin tool the user
configured + capability-filtered, with the capability-resolver attached
to `session.capability_resolver` for the tool router and policy engine.

Reads:
    config.tools_enabled
    config.extras["compiled_ws"].plugin_include / .plugin_exclude (optional)
    workspace config.toml's `[plugins] load_builtins` flag
    workspace config.toml's `[capabilities] grant/deny` (via
        resolve_allowed_tools_from_config)

Writes:
    session.registry — adds plugin tool specs (with capability backfilled)
    session.capability_resolver — exposed for downstream blocks
        (install_tool_router, install_system_tools)

Resources: none (plugins are just specs; no long-lived handles)

Opt-out:
    1. config.tools_enabled is False → return immediately, no tools
    2. workspace `plugins.load_builtins=False` → return immediately
    3. capability grant filter excludes all builtins → tool list is empty

Error model: soft. Per-spec failures log+skip; the block does not raise
on a single bad plugin (matches REPL's prior tolerance).

Replaces these legacy callsites (all DELETED in same change):
    - obscura/cli/_repl_loop.py:335-381 (4 try blocks)
    - obscura/cli/_repl_loop.py:560-582 (PluginLoader cap-index rebuild)
    - obscura/cli/session.py:1260-1310 + 1505-1536 (duplicate REPL paths)
    - obscura/agent/agents.py:431-476 (capability resolver block)

NOT replaced (legitimately separate concerns):
    - obscura/cli/commands.py — `/plugins` slash commands inspect; do
      not register tools. They keep direct PluginLoader use.
    - obscura/core/workspace.py:296-344 — `bootstrap_all_builtins()`
      runs at workspace init time to install plugin pip deps; no
      AgentSession exists in that scope.
"""

from __future__ import annotations

import dataclasses
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from obscura.composition.session import AgentSession, SessionConfig

logger = logging.getLogger(__name__)


async def install_plugin_tools(
    session: AgentSession,
    config: SessionConfig,
) -> None:
    """Register builtin plugin tools onto `session.registry` and build
    the capability resolver onto `session.capability_resolver`.

    See module docstring for full contract.
    """
    if not config.tools_enabled:
        logger.debug("install_plugin_tools: tools disabled, skipping")
        return

    # Workspace flag: `[plugins] load_builtins = false` → no plugin tools
    from obscura.plugins.loader import _load_plugin_config_flag  # pyright: ignore[reportPrivateUsage]

    if not _load_plugin_config_flag("load_builtins", default=True):
        logger.debug("install_plugin_tools: plugins.load_builtins=False, skipping")
        return

    # ── 1. Build capability resolver (used by tool router + system tools) ──
    try:
        from obscura.plugins.capabilities import CapabilityResolver
        from obscura.plugins.loader import PluginLoader
        from obscura.plugins.registries.capability_index import CapabilityIndex
        from obscura.plugins.registries.tool_index import ToolIndex

        loader = PluginLoader()
        cap_index = CapabilityIndex()
        tool_idx = ToolIndex()

        for spec in loader.discover_builtins() + loader.discover_local():
            for cap in spec.capabilities:
                cap_index.register(cap, spec.id)
            for tool_contrib in spec.tools:
                tool_idx.register(tool_contrib, spec.id)

        resolver = CapabilityResolver(cap_index, tool_idx)
        resolver.grant_defaults(session.session_id)
        session.capability_resolver = resolver
    except Exception:
        logger.exception("install_plugin_tools: capability resolver build failed")
        # Continue without resolver — plugin tools still register

    # ── 2. Discover + filter plugin tool specs ─────────────────────────
    compiled_ws = config.extras.get("compiled_ws")
    ws_include = getattr(compiled_ws, "plugin_include", None) if compiled_ws else None
    ws_exclude = getattr(compiled_ws, "plugin_exclude", None) if compiled_ws else None

    try:
        from obscura.plugins.loader import (
            get_all_builtin_tool_specs,
            get_filtered_builtin_tool_specs,
        )

        if ws_include or ws_exclude:
            plugin_specs: list[Any] = get_filtered_builtin_tool_specs(
                ws_include, ws_exclude,
            )
        else:
            plugin_specs = get_all_builtin_tool_specs()
    except Exception:
        logger.exception("install_plugin_tools: spec discovery failed")
        return

    # ── 3. Backfill capability field, filter by grants, register ───────
    try:
        from obscura.plugins.capabilities import resolve_allowed_tools_from_config
        from obscura.plugins.loader import get_capability_map

        cap_map = get_capability_map()
        allowed = resolve_allowed_tools_from_config()
    except Exception:
        logger.exception("install_plugin_tools: capability map / grants failed")
        cap_map = {}
        allowed = None

    registered = 0
    skipped = 0
    for spec in plugin_specs:
        # Backfill capability field from the loader's capability map
        cap = cap_map.get(spec.name, getattr(spec, "capability", ""))
        if cap and not getattr(spec, "capability", ""):
            try:
                spec = dataclasses.replace(spec, capability=cap)
            except Exception:
                logger.debug(
                    "install_plugin_tools: capability backfill failed for %s",
                    spec.name,
                    exc_info=True,
                )
        # Filter by capability grants
        if cap and allowed is not None and spec.name not in allowed:
            skipped += 1
            continue
        if session.add_tool(spec):
            registered += 1
        else:
            skipped += 1

    logger.info(
        "install_plugin_tools: registered=%d skipped=%d (surface=%s)",
        registered,
        skipped,
        session.surface,
    )
