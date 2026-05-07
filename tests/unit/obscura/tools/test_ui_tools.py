"""Unit tests for user-interaction tools (ask_user, user_interact, user_ask)."""
# pyright: reportPrivateUsage=false

from __future__ import annotations

import json
from typing import Any

import pytest

from obscura.core.tool_context import ToolContext, bind_tool_context
from obscura.tools.system._ui import UI

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_ui() -> object:
    """Ensure no module-level callbacks leak between tests."""
    UI.ask_user_callback = None
    UI.user_interact_callback = None
    UI.ask_user_called = False
    yield
    UI.ask_user_callback = None
    UI.user_interact_callback = None
    UI.ask_user_called = False


# ---------------------------------------------------------------------------
# ask_user
# ---------------------------------------------------------------------------


async def test_ask_user_uses_context_callback() -> None:
    async def cb(**_: Any) -> str:
        return "yes"

    ctx = ToolContext(ask_user_callback=cb)
    with bind_tool_context(ctx):
        result = json.loads(await UI.ask_user(question="Continue?"))

    assert result["ok"] is True
    assert result["selected"] == "yes"


async def test_ask_user_falls_back_to_module_callback() -> None:
    async def cb(**_: Any) -> str:
        return "module_answer"

    UI.ask_user_callback = cb
    result = json.loads(await UI.ask_user(question="Go?"))

    assert result["ok"] is True
    assert result["selected"] == "module_answer"


async def test_ask_user_context_callback_takes_priority_over_module() -> None:
    async def ctx_cb(**_: Any) -> str:
        return "from_context"

    async def mod_cb(**_: Any) -> str:
        return "from_module"

    UI.ask_user_callback = mod_cb
    ctx = ToolContext(ask_user_callback=ctx_cb)
    with bind_tool_context(ctx):
        result = json.loads(await UI.ask_user(question="?"))

    assert result["selected"] == "from_context"


async def test_ask_user_no_callback_returns_no_ui_error() -> None:
    result = json.loads(await UI.ask_user(question="Anything?"))
    assert result["ok"] is False
    assert result.get("error") == "no_ui"


async def test_ask_user_callback_exception_returns_error() -> None:
    async def boom(**_: Any) -> str:
        raise RuntimeError("oops")

    ctx = ToolContext(ask_user_callback=boom)
    with bind_tool_context(ctx):
        result = json.loads(await UI.ask_user(question="?"))

    assert result["ok"] is False
    assert "ask_user_failed" in result.get("error", "")


async def test_ask_user_passes_choices_to_callback() -> None:
    received: dict[str, Any] = {}

    async def cb(**kwargs: Any) -> str:
        received.update(kwargs)
        choices = kwargs.get("choices", [])
        return choices[0] if choices else ""

    ctx = ToolContext(ask_user_callback=cb)
    with bind_tool_context(ctx):
        await UI.ask_user(question="Pick:", choices=["a", "b"])

    assert received.get("choices") == ["a", "b"]


async def test_ask_user_sets_called_flag() -> None:
    async def cb(**_: Any) -> str:
        return "x"

    UI.ask_user_callback = cb
    UI.ask_user_called = False
    await UI.ask_user(question="?")
    assert UI.ask_user_called is True


# ---------------------------------------------------------------------------
# user_interact — question mode
# ---------------------------------------------------------------------------


async def test_user_interact_question_mode_with_callback() -> None:
    async def cb(**kwargs: Any) -> dict[str, Any]:
        return {"selected": "choice_a"}

    ctx = ToolContext(user_interact_callback=cb)
    with bind_tool_context(ctx):
        result = json.loads(
            await UI.user_interact(
                mode="question",
                question="Pick one?",
                choices=["choice_a", "choice_b"],
            )
        )

    assert result["ok"] is True
    assert result["selected"] == "choice_a"


async def test_user_interact_question_mode_no_callback_returns_error() -> None:
    result = json.loads(
        await UI.user_interact(mode="question", question="Pick?", choices=["x"])
    )
    assert result["ok"] is False


# ---------------------------------------------------------------------------
# user_interact — permission mode
# ---------------------------------------------------------------------------


async def test_user_interact_permission_mode_approved() -> None:
    async def cb(**_kwargs: Any) -> dict[str, Any]:
        return {"approved": True}

    ctx = ToolContext(user_interact_callback=cb)
    with bind_tool_context(ctx):
        result = json.loads(
            await UI.user_interact(
                mode="permission",
                action="delete file",
                reason="clean up",
                risk="low",
            )
        )

    assert result["ok"] is True
    assert result["approved"] is True


async def test_user_interact_permission_mode_denied() -> None:
    async def cb(**_kwargs: Any) -> dict[str, Any]:
        return {"approved": False}

    ctx = ToolContext(user_interact_callback=cb)
    with bind_tool_context(ctx):
        result = json.loads(
            await UI.user_interact(
                mode="permission",
                action="delete file",
                reason="clean up",
                risk="low",
            )
        )

    assert result["ok"] is True
    assert result["approved"] is False
