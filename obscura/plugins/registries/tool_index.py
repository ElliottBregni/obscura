"""Tool index — thin wrapper around the existing ToolRegistry."""

from __future__ import annotations

import logging
from typing import Any

from obscura.plugins.models import ToolContribution

logger = logging.getLogger(__name__)


class ToolIndex:
    """In-memory index of tools contributed by plugins.

    This wraps (not replaces) the existing ``ToolRegistry`` from
    ``obscura/core/tools.py``. It adds plugin ownership tracking and
    capability-based filtering.
    """

    def __init__(self) -> None:
        self._tools: dict[str, ToolContribution] = {}
        self._owner: dict[str, str] = {}  # tool_name → plugin_id

    def register(self, contrib: ToolContribution, plugin_id: str) -> None:
        if contrib.name in self._tools and self._owner.get(contrib.name) != plugin_id:
            logger.warning(
                "Tool %s already registered by plugin %s, overwritten by %s",
                contrib.name, self._owner.get(contrib.name), plugin_id,
            )
        self._tools[contrib.name] = contrib
        self._owner[contrib.name] = plugin_id

    def get(self, tool_name: str) -> ToolContribution | None:
        return self._tools.get(tool_name)

    def get_owner(self, tool_name: str) -> str | None:
        return self._owner.get(tool_name)

    def list_all(self) -> list[ToolContribution]:
        return list(self._tools.values())

    def names(self) -> list[str]:
        return list(self._tools.keys())

    def filter_by_plugin(self, plugin_id: str) -> list[ToolContribution]:
        return [
            t for name, t in self._tools.items()
            if self._owner.get(name) == plugin_id
        ]

    def filter_by_capability(self, capability_id: str) -> list[ToolContribution]:
        return [t for t in self._tools.values() if t.capability == capability_id]

    def filter_by_side_effects(self, *effects: str) -> list[ToolContribution]:
        return [t for t in self._tools.values() if t.side_effects in effects]

    def visible_for_capabilities(self, granted: set[str]) -> list[ToolContribution]:
        """Return tools whose capability is in the granted set, or tools with no capability."""
        return [
            t for t in self._tools.values()
            if not t.capability or t.capability in granted
        ]

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, tool_name: str) -> bool:
        return tool_name in self._tools
