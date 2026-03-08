from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest

from obscura.cli.__init__ import _cli_confirm
from obscura.cli.commands import REPLContext, cmd_confirm
from obscura.cli.widgets import WidgetResult


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


@pytest.mark.asyncio
async def test_confirm_always_short_circuit(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _ctx()
    ctx.confirm_always.add("write_file")
    called = {"n": 0}

    async def _fake_confirm(_req: object) -> WidgetResult:
        called["n"] += 1
        return WidgetResult(action="deny")

    monkeypatch.setattr("obscura.cli.widgets.confirm_tool", _fake_confirm)
    approved = await _cli_confirm(ctx, "write_file", {"path": "a.txt"})
    assert approved is True
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_confirm_allow(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _ctx()

    async def _fake_confirm(_req: object) -> WidgetResult:
        return WidgetResult(action="allow")

    monkeypatch.setattr("obscura.cli.widgets.confirm_tool", _fake_confirm)
    approved = await _cli_confirm(ctx, "edit_file", {"path": "x.py"})
    assert approved is True


@pytest.mark.asyncio
async def test_confirm_deny(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _ctx()

    async def _fake_confirm(_req: object) -> WidgetResult:
        return WidgetResult(action="deny")

    monkeypatch.setattr("obscura.cli.widgets.confirm_tool", _fake_confirm)
    approved = await _cli_confirm(ctx, "edit_file", {"path": "x.py"})
    assert approved is False


@pytest.mark.asyncio
async def test_confirm_always_allow_persists(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _ctx()

    async def _fake_confirm(_req: object) -> WidgetResult:
        return WidgetResult(action="always_allow")

    monkeypatch.setattr("obscura.cli.widgets.confirm_tool", _fake_confirm)
    approved = await _cli_confirm(ctx, "edit_file", {"path": "x.py"})
    assert approved is True
    assert "edit_file" in ctx.confirm_always


@pytest.mark.asyncio
async def test_cmd_confirm_subcommands() -> None:
    ctx = _ctx()
    # confirm_always is the active set
    await cmd_confirm("clear", ctx)
    assert not ctx.confirm_always
