"""obscura.providers._tool_host — Shared tool-registration mixin for backends.

Every concrete backend (Claude, Copilot, Codex, OpenAI, LocalLLM, ...) keeps:

* an ordered list of registered :class:`ToolSpec` instances, used to build
  the system-prompt tool listing and the SDK-side MCP server, and
* a :class:`ToolRegistry`, used by the agent loop to look up handlers
  (with alias resolution, name sanitization, etc.).

The duplicate-by-name guard and the dual-add into both stores were
copy-pasted across all five backends. This mixin owns the data structures
and the registration method so concrete backends only need a single
``_init_tool_host()`` call in their ``__init__`` to opt in.

Concrete backend ``__init__`` looks like:

    class FooBackend(BackendToolHostMixin):
        def __init__(self, ...) -> None:
            self._init_tool_host()
            ...
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from obscura.core.tools import ToolRegistry

if TYPE_CHECKING:
    from obscura.core.types import ToolSpec
    from obscura.integrations.mcp.discovery import DiscoveryReport


class BackendToolHostMixin:
    """Mixin: own ``_tools`` and ``_tool_registry`` plus ``register_tool``.

    Concrete backends MUST call :meth:`_init_tool_host` in their
    ``__init__`` before registering any tools. The two attributes
    (``_tools`` / ``_tool_registry``) are intentionally exposed directly
    so call sites that read them (system-prompt builders, etc.) keep
    working unchanged after migration.

    ``last_mcp_discovery_report`` is set by
    :func:`obscura.integrations.mcp.discovery.register_external_mcp_tools`
    after each probe, so callers (and the ``mcp_discovery_status`` system
    tool) can inspect per-server outcomes without scraping log lines.
    """

    _tools: list[ToolSpec]
    _tool_registry: ToolRegistry
    last_mcp_discovery_report: DiscoveryReport | None

    def _init_tool_host(self) -> None:
        """Initialize the empty tool list and registry. Idempotent."""
        if not hasattr(self, "_tools"):
            self._tools = []
        if not hasattr(self, "_tool_registry"):
            self._tool_registry = ToolRegistry()
        if not hasattr(self, "last_mcp_discovery_report"):
            self.last_mcp_discovery_report = None

    def register_tool(self, spec: ToolSpec) -> None:
        """Register *spec* for use in sessions, skipping duplicates by name.

        The same spec is added to the ordered list (used for system-prompt
        building) and to the alias-aware :class:`ToolRegistry` (used for
        agent-loop dispatch).
        """
        if any(t.name == spec.name for t in self._tools):
            return
        self._tools.append(spec)
        self._tool_registry.register(spec)

    def get_tool_registry(self) -> ToolRegistry:
        """Return the registry. Convenience for callers using the older API."""
        return self._tool_registry

    @property
    def tool_specs(self) -> tuple[ToolSpec, ...]:
        """Immutable snapshot of registered specs in registration order."""
        return tuple(self._tools)
