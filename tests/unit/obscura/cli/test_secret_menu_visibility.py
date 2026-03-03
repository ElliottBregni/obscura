from __future__ import annotations

from unittest.mock import Mock

import pytest

from obscura.cli.commands import (
    COMPLETIONS,
    REPLContext,
    cmd_secret,
    set_secret_menu_visibility,
)


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


def test_secret_menu_completions_locked() -> None:
    set_secret_menu_visibility(False)
    assert COMPLETIONS["secret"] == ["status", "unlock", "lock"]
    assert "loglevel" not in COMPLETIONS
    assert "jitter" not in COMPLETIONS


def test_secret_menu_completions_unlocked() -> None:
    set_secret_menu_visibility(True)
    assert "loglevel" in COMPLETIONS
    assert "jitter" in COMPLETIONS
    assert "loglevel" in COMPLETIONS["secret"]
    assert "jitter" in COMPLETIONS["secret"]


@pytest.mark.asyncio
async def test_cmd_secret_unlock_lock_toggles_completions(monkeypatch) -> None:
    ctx = _ctx()
    set_secret_menu_visibility(False)
    ctx.secret_menu_unlocked = False

    await cmd_secret("unlock", ctx)
    assert ctx.secret_menu_unlocked is True
    assert "loglevel" in COMPLETIONS
    assert "jitter" in COMPLETIONS

    await cmd_secret("lock", ctx)
    assert ctx.secret_menu_unlocked is False
    assert "loglevel" not in COMPLETIONS
    assert "jitter" not in COMPLETIONS

