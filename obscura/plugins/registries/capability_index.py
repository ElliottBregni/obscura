"""Capability index — tracks declared capabilities and their tool mappings."""

from __future__ import annotations

import logging
from typing import Any

from obscura.plugins.models import CapabilitySpec

logger = logging.getLogger(__name__)


class CapabilityIndex:
    """In-memory index of capabilities contributed by plugins."""

    def __init__(self) -> None:
        self._capabilities: dict[str, CapabilitySpec] = {}
        self._owner: dict[str, str] = {}  # capability_id → plugin_id

    def register(self, spec: CapabilitySpec, plugin_id: str) -> None:
        if spec.id in self._capabilities and self._owner.get(spec.id) != plugin_id:
            logger.warning(
                "Capability %s already registered by plugin %s, overwritten by %s",
                spec.id, self._owner.get(spec.id), plugin_id,
            )
        self._capabilities[spec.id] = spec
        self._owner[spec.id] = plugin_id

    def get(self, capability_id: str) -> CapabilitySpec | None:
        return self._capabilities.get(capability_id)

    def get_owner(self, capability_id: str) -> str | None:
        return self._owner.get(capability_id)

    def list_all(self) -> list[CapabilitySpec]:
        return list(self._capabilities.values())

    def tools_for_capability(self, capability_id: str) -> tuple[str, ...]:
        cap = self._capabilities.get(capability_id)
        return cap.tools if cap else ()

    def tools_for_capabilities(self, capability_ids: set[str]) -> set[str]:
        tools: set[str] = set()
        for cid in capability_ids:
            tools.update(self.tools_for_capability(cid))
        return tools

    def capabilities_requiring_approval(self) -> list[CapabilitySpec]:
        return [c for c in self._capabilities.values() if c.requires_approval]

    def capabilities_with_default_grant(self) -> list[CapabilitySpec]:
        return [c for c in self._capabilities.values() if c.default_grant]

    def filter_by_plugin(self, plugin_id: str) -> list[CapabilitySpec]:
        return [
            cap for cid, cap in self._capabilities.items()
            if self._owner.get(cid) == plugin_id
        ]

    def __len__(self) -> int:
        return len(self._capabilities)

    def __contains__(self, capability_id: str) -> bool:
        return capability_id in self._capabilities
