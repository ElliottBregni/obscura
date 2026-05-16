"""obscura.cli.renderer.adapters.tool_mcp — MCP tool refinement.

Tools registered via :func:`register_external_mcp_tools` follow the
``mcp__<server>__<tool>`` naming convention. This adapter recognises
that prefix and refines the :class:`UiEvent` to surface the MCP
server name and the underlying tool separately, plus tags the
``provider`` field so debug output is filterable.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import override

from obscura.cli.renderer.adapters.base import EventAdapter
from obscura.cli.renderer.adapters.runtime import RuntimeEventAdapter
from obscura.cli.renderer.ui_event import UiEvent, UiEventKind
from obscura.core.enums.agent import AgentEventKind
from obscura.core.types import AgentEvent

_MCP_PREFIX = "mcp__"


class MCPToolEventAdapter(EventAdapter):
    """Refines tool_call/tool_result events emitted by MCP-bridged tools."""

    def __init__(self) -> None:
        self._fallback = RuntimeEventAdapter()

    @override
    def handles(self, event: AgentEvent) -> bool:
        if event.kind not in (
            AgentEventKind.TOOL_CALL,
            AgentEventKind.TOOL_RESULT,
            AgentEventKind.TOOL_CALL_FAILURE,
        ):
            return False
        name = event.tool_name or ""
        return name.startswith(_MCP_PREFIX)

    @override
    def adapt(self, event: AgentEvent) -> Iterable[UiEvent]:
        # Delegate the bulk of the projection to the runtime adapter,
        # then patch in MCP-specific metadata + a friendlier title.
        for ui in self._fallback.adapt(event):
            server, tool = _split_mcp_name(event.tool_name or "")
            ui.provider = f"mcp:{server}" if server else "mcp"
            ui.title = tool or ui.title
            ui.metadata = {
                **ui.metadata,
                "mcp_server": server,
                "mcp_tool": tool,
                "transport": "mcp",
            }
            # Tool calls/results from MCP are always normal-visible —
            # they're real user-actionable work, not debug noise.
            if ui.kind in (UiEventKind.TOOL_CALL, UiEventKind.TOOL_RESULT):
                pass  # visibility already NORMAL from runtime adapter
            yield ui


def _split_mcp_name(name: str) -> tuple[str, str]:
    """Parse ``mcp__<server>__<tool>`` into ``(server, tool)``.

    Falls back to ``("", name)`` for malformed input — adapters never
    raise.
    """
    if not name.startswith(_MCP_PREFIX):
        return "", name
    rest = name[len(_MCP_PREFIX) :]
    parts = rest.split("__", 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return rest, ""
