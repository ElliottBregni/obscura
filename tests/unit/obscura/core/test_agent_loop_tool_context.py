"""AgentLoop binds ToolContext around every tool invocation.

Lighter-weight than a full backend round-trip — drives the loop's
private tool-execution path with a stub ToolCallInfo and verifies
the handler sees the context bound.
"""

from __future__ import annotations

import pytest

from obscura.core.agent_loop import AgentLoop
from obscura.core.tool_context import current_tool_context
from obscura.core.tools import ToolRegistry, tool
from obscura.core.types import ToolCallInfo


@pytest.mark.asyncio
async def test_handler_sees_bound_tool_context() -> None:
    """A tool handler invoked by AgentLoop sees a ToolContext bound to the loop's registry."""
    captured: dict[str, object] = {}

    @tool("captures_ctx", "capture the bound ToolContext")
    def captures_ctx() -> str:
        ctx = current_tool_context()
        captured["registry"] = ctx.registry if ctx else None
        captured["history"] = ctx.history if ctx else None
        return "ok"

    registry = ToolRegistry()
    registry.register(captures_ctx.spec)

    loop = AgentLoop(backend=None, tool_registry=registry)
    history: list[dict[str, str]] = [{"role": "user", "content": "hi"}]
    loop._current_messages = history

    tc = ToolCallInfo(tool_use_id="call-1", name="captures_ctx", input={})
    envelope = await loop._execute_single_tool(tc, seen_calls={})

    assert envelope.status == "ok"
    assert captured["registry"] is registry
    # The exact list reference (mutability) is what enables history_snip.
    assert captured["history"] is history


@pytest.mark.asyncio
async def test_handler_sees_host_callbacks_from_globals() -> None:
    """Legacy ``set_*_callback`` globals are surfaced in the bound ToolContext."""
    from obscura.tools import system as system_mod

    seen: dict[str, object] = {}

    @tool("captures_callbacks", "capture host callbacks")
    def captures_callbacks() -> str:
        ctx = current_tool_context()
        seen["ask_user"] = ctx.ask_user_callback if ctx else None
        seen["mode_cb"] = ctx.permission_mode_callback if ctx else None
        return "ok"

    def _ask(question: str, choices: list[str], allow_custom: bool) -> str:
        return ""

    def _mode(mode: str) -> None:
        pass

    prev_ask = system_mod._ask_user_callback
    prev_mode = system_mod._set_permission_mode_callback
    system_mod._ask_user_callback = _ask
    system_mod._set_permission_mode_callback = _mode
    try:
        registry = ToolRegistry()
        registry.register(captures_callbacks.spec)
        loop = AgentLoop(backend=None, tool_registry=registry)

        tc = ToolCallInfo(tool_use_id="c", name="captures_callbacks", input={})
        envelope = await loop._execute_single_tool(tc, seen_calls={})
    finally:
        system_mod._ask_user_callback = prev_ask
        system_mod._set_permission_mode_callback = prev_mode

    assert envelope.status == "ok"
    assert seen["ask_user"] is _ask
    assert seen["mode_cb"] is _mode


@pytest.mark.asyncio
async def test_handler_sees_mcp_discovery_report_from_backend() -> None:
    """The MCP discovery report stashed on the backend reaches the bound ToolContext."""
    from obscura.integrations.mcp.discovery import DiscoveryReport, DiscoveryStatus

    seen: dict[str, object] = {}

    @tool("captures_report", "capture report")
    def captures_report() -> str:
        ctx = current_tool_context()
        seen["report"] = ctx.mcp_discovery_report if ctx else None
        return ""

    class _StubBackend:
        last_mcp_discovery_report = DiscoveryReport(
            statuses=(
                DiscoveryStatus(
                    server_name="x", transport="stdio", ok=True, tool_count=2
                ),
            ),
        )

    registry = ToolRegistry()
    registry.register(captures_report.spec)
    loop = AgentLoop(backend=_StubBackend(), tool_registry=registry)

    tc = ToolCallInfo(tool_use_id="c", name="captures_report", input={})
    await loop._execute_single_tool(tc, seen_calls={})

    report = seen["report"]
    assert report is not None
    assert getattr(report, "total_tools", None) == 2


@pytest.mark.asyncio
async def test_successful_tool_recorded_for_correction_check() -> None:
    """Successful tool calls are tracked so the post-turn scanner can compare them
    against the model's narration."""

    @tool("works", "always succeeds")
    def works() -> str:
        return '{"ok": true, "result": "data"}'

    registry = ToolRegistry()
    registry.register(works.spec)
    loop = AgentLoop(backend=None, tool_registry=registry)
    loop._this_turn_successful_tools = []

    tc = ToolCallInfo(tool_use_id="c", name="works", input={})
    envelope = await loop._execute_single_tool(tc, seen_calls={})

    assert envelope.status == "ok"
    assert len(loop._this_turn_successful_tools) == 1
    summary = loop._this_turn_successful_tools[0]
    assert summary.tool_name == "works"
    assert "data" in summary.snippet


@pytest.mark.asyncio
async def test_context_unbinds_after_tool_returns() -> None:
    """Bound context is reset after a tool call completes."""

    @tool("nop", "do nothing")
    def nop() -> str:
        return ""

    registry = ToolRegistry()
    registry.register(nop.spec)
    loop = AgentLoop(backend=None, tool_registry=registry)

    tc = ToolCallInfo(tool_use_id="c", name="nop", input={})
    await loop._execute_single_tool(tc, seen_calls={})

    # After the tool call, no context should be bound on the calling task.
    assert current_tool_context() is None
