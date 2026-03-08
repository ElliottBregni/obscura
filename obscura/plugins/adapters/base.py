"""Base adapter protocol for Obscura plugin adapters."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from obscura.plugins.models import PluginSpec


@runtime_checkable
class PluginAdapter(Protocol):
    """Protocol that all plugin adapters must implement.

    An adapter bridges a specific packaging/runtime type into the
    normalized resource model.  It handles loading, health checking,
    and teardown for its type.
    """

    def can_handle(self, spec: PluginSpec) -> bool:
        """Return True if this adapter can load the given plugin."""
        ...

    async def load(self, spec: PluginSpec, config: dict[str, Any]) -> dict[str, Any]:
        """Load the plugin and return normalized resources.

        Returns a dict with optional keys:
            - "handlers": dict[str, callable]  — tool name → handler
            - "workflows": list of executable workflow objects
            - "instructions": already captured in spec
        """
        ...

    async def healthcheck(self, spec: PluginSpec) -> bool:
        """Check if the plugin is healthy."""
        ...

    async def teardown(self, spec: PluginSpec) -> None:
        """Clean up resources for the plugin."""
        ...
