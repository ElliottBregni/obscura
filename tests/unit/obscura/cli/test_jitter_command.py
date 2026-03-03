from __future__ import annotations

from unittest.mock import Mock

import pytest

from obscura.cli.commands import REPLContext, cmd_jitter


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
    )


@pytest.mark.asyncio
async def test_jitter_show(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setenv("OBSCURA_REASONING_JITTER_MS", "180")
    monkeypatch.setattr("obscura.cli.commands.print_info", lambda msg: calls.append(msg))
    await cmd_jitter("", _ctx())
    assert any("180ms" in c for c in calls)


@pytest.mark.asyncio
async def test_jitter_set_numeric(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr("obscura.cli.commands.print_ok", lambda msg: calls.append(msg))
    await cmd_jitter("275", _ctx())
    assert any("275" in c for c in calls)


@pytest.mark.asyncio
async def test_jitter_off(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr("obscura.cli.commands.print_ok", lambda msg: calls.append(msg))
    await cmd_jitter("off", _ctx())
    assert any("0ms" in c for c in calls)


@pytest.mark.asyncio
async def test_jitter_invalid(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr("obscura.cli.commands.print_error", lambda msg: calls.append(msg))
    await cmd_jitter("wat", _ctx())
    assert calls and "Usage" in calls[0]

