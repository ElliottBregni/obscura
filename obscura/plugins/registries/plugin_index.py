"""Plugin index — tracks installed/enabled plugin metadata."""

from __future__ import annotations

import logging
from typing import Any

from obscura.plugins.models import PluginSpec, PluginStatus

logger = logging.getLogger(__name__)


class PluginIndex:
    """In-memory index of registered plugins."""

    def __init__(self) -> None:
        self._specs: dict[str, PluginSpec] = {}
        self._statuses: dict[str, PluginStatus] = {}

    def register(self, spec: PluginSpec, status: PluginStatus | None = None) -> None:
        self._specs[spec.id] = spec
        if status:
            self._statuses[spec.id] = status

    def get(self, plugin_id: str) -> PluginSpec | None:
        return self._specs.get(plugin_id)

    def get_status(self, plugin_id: str) -> PluginStatus | None:
        return self._statuses.get(plugin_id)

    def set_status(self, plugin_id: str, status: PluginStatus) -> None:
        self._statuses[plugin_id] = status

    def list_all(self) -> list[PluginSpec]:
        return list(self._specs.values())

    def list_enabled(self) -> list[PluginSpec]:
        return [
            spec for pid, spec in self._specs.items()
            if self._statuses.get(pid, PluginStatus(plugin_id=pid)).enabled
        ]

    def filter_by_trust(self, *levels: str) -> list[PluginSpec]:
        return [s for s in self._specs.values() if s.trust_level in levels]

    def filter_by_runtime(self, *types: str) -> list[PluginSpec]:
        return [s for s in self._specs.values() if s.runtime_type in types]

    def __len__(self) -> int:
        return len(self._specs)

    def __contains__(self, plugin_id: str) -> bool:
        return plugin_id in self._specs
