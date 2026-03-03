from __future__ import annotations

from unittest.mock import Mock

import pytest

from obscura.cli.__init__ import _cli_confirm
from obscura.cli.commands import REPLContext, cmd_confirm


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
async def test_confirm_policy_allowlist_short_circuit(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _ctx()
    ctx.confirm_allow.add("write_file")
    called = {"n": 0}

    async def _fake_prompt(_msg: str) -> str:
        called["n"] += 1
        return "deny"

    monkeypatch.setattr("obscura.cli.__init__.confirm_prompt_async", _fake_prompt)
    approved = await _cli_confirm(ctx, "write_file", {"path": "a.txt"})
    assert approved is True
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_confirm_policy_denylist_short_circuit(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _ctx()
    ctx.confirm_deny.add("write_file")
    called = {"n": 0}

    async def _fake_prompt(_msg: str) -> str:
        called["n"] += 1
        return "approve"

    monkeypatch.setattr("obscura.cli.__init__.confirm_prompt_async", _fake_prompt)
    approved = await _cli_confirm(ctx, "write_file", {"path": "a.txt"})
    assert approved is False
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_confirm_default_approve_no_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _ctx()
    ctx.confirm_default = "approve"
    called = {"n": 0}

    async def _fake_prompt(_msg: str) -> str:
        called["n"] += 1
        return "deny"

    monkeypatch.setattr("obscura.cli.__init__.confirm_prompt_async", _fake_prompt)
    approved = await _cli_confirm(ctx, "run_shell", {"cmd": "echo hi"})
    assert approved is True
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_confirm_default_deny_no_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _ctx()
    ctx.confirm_default = "deny"
    called = {"n": 0}

    async def _fake_prompt(_msg: str) -> str:
        called["n"] += 1
        return "approve"

    monkeypatch.setattr("obscura.cli.__init__.confirm_prompt_async", _fake_prompt)
    approved = await _cli_confirm(ctx, "run_shell", {"cmd": "echo hi"})
    assert approved is False
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_confirm_prompt_always_persists_allow(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _ctx()

    async def _fake_prompt(_msg: str) -> str:
        return "always"

    monkeypatch.setattr("obscura.cli.__init__.confirm_prompt_async", _fake_prompt)
    approved = await _cli_confirm(ctx, "edit_file", {"path": "x.py"})
    assert approved is True
    assert "edit_file" in ctx.confirm_allow
    assert "edit_file" not in ctx.confirm_deny


@pytest.mark.asyncio
async def test_confirm_prompt_never_persists_deny(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _ctx()

    async def _fake_prompt(_msg: str) -> str:
        return "never"

    monkeypatch.setattr("obscura.cli.__init__.confirm_prompt_async", _fake_prompt)
    approved = await _cli_confirm(ctx, "edit_file", {"path": "x.py"})
    assert approved is False
    assert "edit_file" in ctx.confirm_deny
    assert "edit_file" not in ctx.confirm_allow


@pytest.mark.asyncio
async def test_cmd_confirm_subcommands() -> None:
    ctx = _ctx()
    await cmd_confirm("default approve", ctx)
    assert ctx.confirm_default == "approve"

    await cmd_confirm("allow run_shell", ctx)
    assert "run_shell" in ctx.confirm_allow
    assert "run_shell" not in ctx.confirm_deny

    await cmd_confirm("deny run_shell", ctx)
    assert "run_shell" in ctx.confirm_deny
    assert "run_shell" not in ctx.confirm_allow

    await cmd_confirm("clear deny", ctx)
    assert not ctx.confirm_deny


@pytest.mark.asyncio
async def test_confirm_invalid_then_approve_retries_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _ctx()
    answers = iter(["wat", "approve"])

    async def _fake_prompt(_msg: str) -> str:
        return next(answers)

    monkeypatch.setattr("obscura.cli.__init__.confirm_prompt_async", _fake_prompt)
    approved = await _cli_confirm(ctx, "edit_file", {"path": "x.py"})
    assert approved is True


@pytest.mark.asyncio
async def test_confirm_invalid_twice_denies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _ctx()
    answers = iter(["wat", "still-wat"])

    async def _fake_prompt(_msg: str) -> str:
        return next(answers)

    monkeypatch.setattr("obscura.cli.__init__.confirm_prompt_async", _fake_prompt)
    approved = await _cli_confirm(ctx, "edit_file", {"path": "x.py"})
    assert approved is False
