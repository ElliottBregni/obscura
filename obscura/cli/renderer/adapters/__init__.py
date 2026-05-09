"""obscura.cli.renderer.adapters — Per-source/tool UiEvent adapters.

Provider-specific adapters already exist upstream as the
``StreamChunk`` adapters in :mod:`obscura.core.stream` (Claude,
OpenAI, Copilot, Codex). By the time an event reaches the renderer
it is an :class:`AgentEvent`, which is provider-agnostic.

This package owns the *second* normalization step: shaping each
:class:`AgentEvent` into a stable :class:`UiEvent` with the right
visibility/source classification. Per-tool adapters refine fields
when the tool name implies a known shape (MCP wrapper, shell, etc.).
"""

from __future__ import annotations

from obscura.cli.renderer.adapters.base import EventAdapter
from obscura.cli.renderer.adapters.runtime import RuntimeEventAdapter
from obscura.cli.renderer.adapters.tool_mcp import MCPToolEventAdapter
from obscura.cli.renderer.adapters.tool_shell import ShellToolEventAdapter

__all__ = [
    "EventAdapter",
    "MCPToolEventAdapter",
    "RuntimeEventAdapter",
    "ShellToolEventAdapter",
]
