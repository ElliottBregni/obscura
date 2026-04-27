"""Tests for the user_interact tool."""

from __future__ import annotations

import contextlib
import json
import sys
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from obscura.core.tool_context import ToolContext, bind_tool_context
from obscura.tools.system import user_interact


@contextlib.contextmanager
def _user_interact_ctx(cb: Any) -> Iterator[None]:
    """Bind a ToolContext with the given user_interact callback."""
    with bind_tool_context(ToolContext(user_interact_callback=cb)):
        yield


def _make_callback(return_value: dict[str, Any]) -> AsyncMock:
    return AsyncMock(return_value=return_value)


# ---------------------------------------------------------------------------
# Permission mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_permission_approved() -> None:
    cb = _make_callback({"approved": True})
    with _user_interact_ctx(cb):
        raw = await user_interact(
            mode="permission",
            action="delete file",
            reason="cleanup",
            risk="high",
        )
    result = json.loads(raw)
    assert result["ok"] is True
    assert result["approved"] is True
    assert result["action"] == "approve"


@pytest.mark.asyncio
async def test_permission_denied() -> None:
    cb = _make_callback({"approved": False})
    with _user_interact_ctx(cb):
        raw = await user_interact(
            mode="permission",
            action="drop table",
            reason="migration",
            risk="critical",
        )
    result = json.loads(raw)
    assert result["ok"] is True
    assert result["approved"] is False
    assert result["action"] == "deny"


@pytest.mark.asyncio
async def test_permission_no_callback() -> None:
    raw = await user_interact(mode="permission", action="test", reason="test")
    result = json.loads(raw)
    assert result["ok"] is False
    assert result["error"] == "no_ui"


# ---------------------------------------------------------------------------
# Notify mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notify_tui_channel() -> None:
    cb = _make_callback({})
    with _user_interact_ctx(cb):
        raw = await user_interact(
            mode="notify",
            title="Done",
            message="Task complete",
            priority="normal",
            channels=["tui"],
        )
    result = json.loads(raw)
    assert result["ok"] is True
    assert result["delivered"] is True
    assert "tui" in result["channels"]
    cb.assert_awaited_once()


@pytest.mark.asyncio
async def test_notify_bell_channel() -> None:
    with (
        patch.object(sys.stdout, "write") as mock_write,
        patch.object(sys.stdout, "flush"),
    ):
        raw = await user_interact(
            mode="notify",
            title="Alert",
            message="Check this",
            channels=["bell"],
        )
        result = json.loads(raw)
        assert "bell" in result["channels"]
        mock_write.assert_called_with("\a")


@pytest.mark.asyncio
async def test_notify_os_channel() -> None:
    mock_notifier = AsyncMock()
    mock_notifier.notify = AsyncMock()

    mock_native_mod = type(sys)("obscura.notifications.native")
    mock_native_mod.NativeNotifier = lambda: mock_notifier  # type: ignore[attr-defined]

    # Also need the parent package in sys.modules for the import to resolve
    mock_notifications_pkg = type(sys)("obscura.notifications")

    with patch.dict(
        "sys.modules",
        {
            "obscura.notifications": mock_notifications_pkg,
            "obscura.notifications.native": mock_native_mod,
        },
    ):
        raw = await user_interact(
            mode="notify",
            title="OS Alert",
            message="Check this",
            channels=["os"],
        )
        result = json.loads(raw)
        assert result["ok"] is True
        assert "os" in result["channels"]


@pytest.mark.asyncio
async def test_notify_no_callback_still_delivers_bell() -> None:
    """Notify non-TUI channels work even without a callback."""
    with patch.object(sys.stdout, "write"), patch.object(sys.stdout, "flush"):
        raw = await user_interact(
            mode="notify",
            title="Alert",
            message="Check",
            channels=["bell"],
        )
        result = json.loads(raw)
        assert result["ok"] is True
        assert "bell" in result["channels"]


@pytest.mark.asyncio
async def test_notify_default_channels() -> None:
    """Default channels are tui + bell."""
    cb = _make_callback({})
    with (
        _user_interact_ctx(cb),
        patch.object(sys.stdout, "write"),
        patch.object(sys.stdout, "flush"),
    ):
        raw = await user_interact(
            mode="notify",
            title="Alert",
            message="Check",
        )
        result = json.loads(raw)
        assert result["ok"] is True
        assert "tui" in result["channels"]
        assert "bell" in result["channels"]


# ---------------------------------------------------------------------------
# Question mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_question_with_choices() -> None:
    cb = _make_callback({"selected": "Option B"})
    with _user_interact_ctx(cb):
        raw = await user_interact(
            mode="question",
            question="Pick one",
            choices=["Option A", "Option B"],
        )
    result = json.loads(raw)
    assert result["ok"] is True
    assert result["selected"] == "Option B"


@pytest.mark.asyncio
async def test_question_freetext() -> None:
    cb = _make_callback({"selected": "my custom answer"})
    with _user_interact_ctx(cb):
        raw = await user_interact(mode="question", question="What do you think?")
    result = json.loads(raw)
    assert result["ok"] is True
    assert result["selected"] == "my custom answer"


@pytest.mark.asyncio
async def test_question_no_callback() -> None:
    raw = await user_interact(mode="question", question="Hello?")
    result = json.loads(raw)
    assert result["ok"] is False
    assert result["error"] == "no_ui"


# ---------------------------------------------------------------------------
# Invalid mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalid_mode() -> None:
    raw = await user_interact(mode="bogus")
    result = json.loads(raw)
    assert result["ok"] is False
    assert result["error"] == "invalid_mode"
