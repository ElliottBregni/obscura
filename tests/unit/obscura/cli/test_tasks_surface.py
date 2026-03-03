from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from obscura.cli.__init__ import _parse_confirm_decision, _track_task_surface_event
from obscura.cli.commands import REPLContext, cmd_menu, cmd_tasks
from obscura.core.types import AgentEventKind


def _ctx() -> REPLContext:
    return REPLContext(
        client=Mock(),
        store=Mock(),
        session_id="s1",
        backend="codex",
        model="gpt-5",
        system_prompt="",
        max_turns=8,
        tools_enabled=True,
        confirm_enabled=True,
    )


def test_parse_confirm_accepts_yes_no_sentences() -> None:
    assert _parse_confirm_decision("yes approve this") == "approve"
    assert _parse_confirm_decision("no do not allow") == "deny"


def test_track_task_surface_python_exec_lifecycle() -> None:
    ctx = _ctx()
    call = SimpleNamespace(
        kind=AgentEventKind.TOOL_CALL,
        tool_name="run_shell",
        tool_input={"cmd": "uv run python script.py"},
        tool_use_id="u1",
    )
    _track_task_surface_event(ctx, call)
    assert "u1" in ctx._pending_python_tasks
    assert ctx._pending_python_tasks["u1"]["status"] == "running"

    result = SimpleNamespace(
        kind=AgentEventKind.TOOL_RESULT,
        tool_use_id="u1",
        is_error=False,
    )
    _track_task_surface_event(ctx, result)
    assert "u1" not in ctx._pending_python_tasks
    assert ctx.python_tasks
    assert ctx.python_tasks[-1]["status"] == "done"


@pytest.mark.asyncio
async def test_cmd_tasks_clear() -> None:
    ctx = _ctx()
    ctx.background_tasks.append(
        {"id": "bg-1", "status": "done", "kind": "chat", "preview": "hi"}
    )
    ctx.python_tasks.append(
        {"id": "py-1", "status": "done", "tool": "run_shell", "command": "python"}
    )
    await cmd_tasks("clear", ctx)
    assert not ctx.background_tasks
    assert not ctx.python_tasks


@pytest.mark.asyncio
async def test_cmd_tasks_interrupt_all() -> None:
    class _FakeTask:
        def __init__(self) -> None:
            self.cancelled = False

        def cancel(self) -> None:
            self.cancelled = True

    ctx = _ctx()
    t = _FakeTask()
    ctx.background_tasks.append(
        {
            "id": "bg-1",
            "status": "running",
            "kind": "chat",
            "preview": "long task",
            "started_at": "0.0",
        }
    )
    ctx._background_task_refs["bg-1"] = t
    await cmd_tasks("interrupt all", ctx)
    assert t.cancelled is True
    assert ctx.background_tasks[-1]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_cmd_menu_toggles() -> None:
    ctx = _ctx()
    await cmd_menu("off", ctx)
    assert ctx.ui_right_menu_enabled is False
    await cmd_menu("reasoning off", ctx)
    assert ctx.ui_menu_items["reasoning"] is False
    await cmd_menu("reasoning on", ctx)
    assert ctx.ui_menu_items["reasoning"] is True
