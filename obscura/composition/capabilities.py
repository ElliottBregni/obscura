"""obscura.composition.capabilities — capability resolver discovery.

Shared helper for building a ``CapabilityResolver`` from discovered
plugin specs. Used by:

- ``install_plugin_tools`` block (composition path) — grants default
  capabilities to the session id.
- ``agent/agents.py:Agent.start()`` (legacy path) — needs the resolver
  BEFORE the client is built so the system-prompt's skill gating is
  active during ContextLoader.

Both paths used to inline near-identical PluginLoader → CapabilityIndex →
ToolIndex → CapabilityResolver dance. Now they call ``discover_capabilities``
and apply grantee-specific grants on top.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from obscura.plugins.capabilities import CapabilityResolver

logger = logging.getLogger(__name__)


def discover_capabilities(
    grantee_id: str = "",
    *,
    include_user: bool = False,
) -> CapabilityResolver | None:
    """Build a ``CapabilityResolver`` populated from builtin + local
    plugins (and optionally user plugins).

    Returns ``None`` on any failure during discovery so callers can fall
    back gracefully — matches the legacy try/except suppression pattern
    in ``Agent.start()``.

    The caller is responsible for applying grantee-specific grants/denies
    after the resolver is returned. ``grantee_id`` (when non-empty) gets
    default-grant capabilities applied automatically.
    """
    try:
        from obscura.plugins.capabilities import CapabilityResolver
        from obscura.plugins.loader import PluginLoader
        from obscura.plugins.registries.capability_index import CapabilityIndex
        from obscura.plugins.registries.tool_index import ToolIndex

        loader = PluginLoader()
        cap_index = CapabilityIndex()
        tool_idx = ToolIndex()

        specs = loader.discover_builtins() + loader.discover_local()
        if include_user:
            specs += loader.discover_user()

        for spec in specs:
            for cap in spec.capabilities:
                cap_index.register(cap, spec.id)
            for tool_contrib in spec.tools:
                tool_idx.register(tool_contrib, spec.id)

        resolver = CapabilityResolver(cap_index, tool_idx)
        if grantee_id:
            resolver.grant_defaults(grantee_id)
        return resolver
    except Exception:
        logger.debug("discover_capabilities: failed", exc_info=True)
        return None
