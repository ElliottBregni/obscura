"""Lazy loading infrastructure for Obscura plugins.

Plugins are discovered at boot (manifests read, metadata registered) but
actual initialization is deferred until the first tool call from that plugin.

Lifecycle states::

    discovered → ready → initializing → active → suspended → failed

Usage::

    from obscura.plugins.lazy import LazyPluginManager

    manager = LazyPluginManager(loader)
    manager.discover_all()       # reads manifests, no init
    tool = manager.get_tool("github_search")  # triggers init of github plugin
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from obscura.plugins.models import PluginSpec, PluginStatus

logger = logging.getLogger(__name__)


class LazyState(str, Enum):
    DISCOVERED = "discovered"     # manifest read, metadata registered
    READY = "ready"               # config resolved, can initialize
    INITIALIZING = "initializing" # bootstrap/loading in progress
    ACTIVE = "active"             # fully loaded, tools available
    SUSPENDED = "suspended"       # was active, now suspended (e.g. healthcheck fail)
    FAILED = "failed"             # init failed


@dataclass
class LazyPluginEntry:
    """Tracks lazy state for a single plugin."""
    spec: PluginSpec
    state: LazyState = LazyState.DISCOVERED
    init_callback: Callable[[], Any] | None = None
    error: str = ""
    initialized_at: float = 0.0
    last_used_at: float = 0.0
    use_count: int = 0

    @property
    def tool_names(self) -> set[str]:
        return {t.name for t in self.spec.tools}


class LazyPluginManager:
    """Manages lazy initialization of plugins.

    Parameters
    ----------
    init_fn : callable
        Function that takes a PluginSpec and performs full initialization
        (loading handlers, registering tools, etc.). Called only on first use.
    prewarm : set[str] | None
        Plugin IDs to eagerly initialize at discover time.
    """

    def __init__(
        self,
        init_fn: Callable[[PluginSpec], None],
        prewarm: set[str] | None = None,
    ) -> None:
        self._init_fn = init_fn
        self._prewarm = prewarm or set()
        self._entries: dict[str, LazyPluginEntry] = {}
        self._tool_to_plugin: dict[str, str] = {}  # tool_name → plugin_id

    # -- Discovery ---------------------------------------------------------

    def register(self, spec: PluginSpec, init_callback: Callable[[], Any] | None = None) -> None:
        """Register a discovered plugin without initializing it."""
        entry = LazyPluginEntry(spec=spec, state=LazyState.DISCOVERED, init_callback=init_callback)

        # Map tools to plugin for on-demand init
        for t in spec.tools:
            self._tool_to_plugin[t.name] = spec.id

        self._entries[spec.id] = entry
        entry.state = LazyState.READY
        logger.debug("Registered lazy plugin: %s (%d tools)", spec.id, len(spec.tools))

        # Prewarm if requested
        if spec.id in self._prewarm:
            self._ensure_initialized(spec.id)

    # -- On-demand init ----------------------------------------------------

    def _ensure_initialized(self, plugin_id: str) -> bool:
        """Initialize a plugin if not already active. Returns True on success."""
        entry = self._entries.get(plugin_id)
        if entry is None:
            return False
        if entry.state == LazyState.ACTIVE:
            return True
        if entry.state == LazyState.FAILED:
            return False

        entry.state = LazyState.INITIALIZING
        try:
            self._init_fn(entry.spec)
            entry.state = LazyState.ACTIVE
            entry.initialized_at = time.time()
            logger.info("Lazy-initialized plugin: %s", plugin_id)
            return True
        except Exception as exc:
            entry.state = LazyState.FAILED
            entry.error = str(exc)
            logger.error("Failed to initialize plugin %s: %s", plugin_id, exc)
            return False

    def ensure_tool_ready(self, tool_name: str) -> bool:
        """Ensure the plugin owning a tool is initialized."""
        plugin_id = self._tool_to_plugin.get(tool_name)
        if plugin_id is None:
            return False
        ok = self._ensure_initialized(plugin_id)
        if ok:
            entry = self._entries[plugin_id]
            entry.last_used_at = time.time()
            entry.use_count += 1
        return ok

    # -- Suspension --------------------------------------------------------

    def suspend(self, plugin_id: str) -> bool:
        entry = self._entries.get(plugin_id)
        if entry and entry.state == LazyState.ACTIVE:
            entry.state = LazyState.SUSPENDED
            logger.info("Suspended plugin: %s", plugin_id)
            return True
        return False

    def resume(self, plugin_id: str) -> bool:
        entry = self._entries.get(plugin_id)
        if entry and entry.state == LazyState.SUSPENDED:
            return self._ensure_initialized(plugin_id)
        return False

    # -- Queries -----------------------------------------------------------

    def get_state(self, plugin_id: str) -> LazyState | None:
        entry = self._entries.get(plugin_id)
        return entry.state if entry else None

    def is_active(self, plugin_id: str) -> bool:
        return self.get_state(plugin_id) == LazyState.ACTIVE

    def active_plugins(self) -> list[str]:
        return [pid for pid, e in self._entries.items() if e.state == LazyState.ACTIVE]

    def all_entries(self) -> dict[str, LazyPluginEntry]:
        return dict(self._entries)

    def stats(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for e in self._entries.values():
            counts[e.state.value] = counts.get(e.state.value, 0) + 1
        return counts


__all__ = [
    "LazyState",
    "LazyPluginEntry",
    "LazyPluginManager",
]
