from __future__ import annotations

from unittest.mock import Mock

import pytest

from obscura.cli.__init__ import _parse_confirm_decision
from obscura.cli.commands import REPLContext, cmd_menu


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


@pytest.mark.asyncio
@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_cmd_menu_toggles() -> None:
    ctx = _ctx()
    await cmd_menu("off", ctx)
    assert ctx.ui_right_menu_enabled is False
    await cmd_menu("reasoning off", ctx)
    assert ctx.ui_menu_items["reasoning"] is False
    await cmd_menu("reasoning on", ctx)
    assert ctx.ui_menu_items["reasoning"] is True
