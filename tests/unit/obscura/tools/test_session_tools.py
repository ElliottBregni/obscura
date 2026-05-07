"""Unit tests for session tools (enter/exit_plan_mode, context_window_status, history_snip)."""
# pyright: reportPrivateUsage=false

from __future__ import annotations

import json

import pytest

from obscura.core.tool_context import ToolContext, bind_tool_context
from obscura.tools.system._session import Session

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_session() -> object:
    """Reset all ClassVar state before each test."""
    Session.permission_mode_callback = None
    Session.plan_approval_callback = None
    Session.snip_message_history = None
    Session.token_usage = {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "context_window": 0,
        "compact_threshold": 0,
    }
    yield
    Session.permission_mode_callback = None
    Session.plan_approval_callback = None
    Session.snip_message_history = None


# ---------------------------------------------------------------------------
# enter_plan_mode
# ---------------------------------------------------------------------------


async def test_enter_plan_mode_calls_mode_callback_with_plan() -> None:
    received: list[str] = []

    def cb(mode: str) -> None:
        received.append(mode)

    ctx = ToolContext(permission_mode_callback=cb)
    with bind_tool_context(ctx):
        result = json.loads(await Session.enter_plan_mode())

    assert result["ok"] is True
    assert result["mode"] == "plan"
    assert received == ["plan"]


async def test_enter_plan_mode_no_callback_still_succeeds() -> None:
    result = json.loads(await Session.enter_plan_mode())
    assert result["ok"] is True
    assert result["mode"] == "plan"


async def test_enter_plan_mode_falls_back_to_class_callback() -> None:
    received: list[str] = []

    def cb(mode: str) -> None:
        received.append(mode)

    Session.permission_mode_callback = cb
    result = json.loads(await Session.enter_plan_mode())

    assert result["ok"] is True
    assert received == ["plan"]


# ---------------------------------------------------------------------------
# exit_plan_mode
# ---------------------------------------------------------------------------


async def test_exit_plan_mode_approved_calls_mode_callback() -> None:
    mode_received: list[str] = []

    async def approval_cb(_summary: str) -> bool:
        return True

    def mode_cb(mode: str) -> None:
        mode_received.append(mode)

    ctx = ToolContext(permission_mode_callback=mode_cb, plan_approval_callback=approval_cb)
    with bind_tool_context(ctx):
        result = json.loads(await Session.exit_plan_mode(plan_summary="do things"))

    assert result["ok"] is True
    assert result["mode"] == "default"
    assert mode_received == ["default"]


async def test_exit_plan_mode_denied_returns_error() -> None:
    async def approval_cb(_summary: str) -> bool:
        return False

    ctx = ToolContext(plan_approval_callback=approval_cb)
    with bind_tool_context(ctx):
        result = json.loads(await Session.exit_plan_mode())

    assert result["ok"] is False
    assert result["mode"] == "plan"


async def test_exit_plan_mode_no_approval_callback_succeeds() -> None:
    result = json.loads(await Session.exit_plan_mode())
    assert result["ok"] is True
    assert result["mode"] == "default"


async def test_exit_plan_mode_denied_does_not_call_mode_callback() -> None:
    mode_received: list[str] = []

    async def approval_cb(_summary: str) -> bool:
        return False

    def mode_cb(mode: str) -> None:
        mode_received.append(mode)

    ctx = ToolContext(permission_mode_callback=mode_cb, plan_approval_callback=approval_cb)
    with bind_tool_context(ctx):
        await Session.exit_plan_mode()

    assert mode_received == []  # mode stays "plan", callback never fired


# ---------------------------------------------------------------------------
# context_window_status
# ---------------------------------------------------------------------------


async def test_context_window_status_returns_token_counts() -> None:
    Session.update_token_usage(input_tokens=1000, output_tokens=500, context_window=8192)

    result = json.loads(await Session.context_window_status())

    assert result["ok"] is True
    assert result["input_tokens"] == 1000
    assert result["output_tokens"] == 500
    assert result["total_tokens"] == 1500
    assert result["context_window"] == 8192


async def test_context_window_status_percent_used() -> None:
    Session.update_token_usage(input_tokens=4000, output_tokens=0, context_window=8000)

    result = json.loads(await Session.context_window_status())

    assert result["percent_used"] == pytest.approx(50.0, abs=1.0)


# ---------------------------------------------------------------------------
# history_snip
# ---------------------------------------------------------------------------


async def test_history_snip_removes_specified_range() -> None:
    history: list[dict[str, str]] = [{"role": "user", "content": str(i)} for i in range(6)]
    ctx = ToolContext(history=history)
    with bind_tool_context(ctx):
        result = json.loads(await Session.history_snip(start_turn=1, end_turn=3))

    assert result["ok"] is True
    # turns 1-3 removed → 3 items remain (0, 4, 5)
    assert len(history) == 3


async def test_history_snip_no_history_returns_error() -> None:
    # No ctx, no Session.snip_message_history → error
    result = json.loads(await Session.history_snip(start_turn=0, end_turn=1))
    assert result["ok"] is False
    assert "no_history" in result.get("error", "")


async def test_history_snip_uses_session_fallback() -> None:
    history: list[dict[str, str]] = [{"role": "user", "content": str(i)} for i in range(4)]
    Session.snip_message_history = history

    result = json.loads(await Session.history_snip(start_turn=0, end_turn=1))

    assert result["ok"] is True
    assert len(history) == 2
