"""Unit tests for ClaudeBackend permission-mode callback wiring."""
# pyright: reportPrivateUsage=false

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from obscura.core.auth import AuthConfig
from obscura.providers.claude import ClaudeBackend

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_backend() -> ClaudeBackend:
    return ClaudeBackend(AuthConfig())


def _extract_can_use_tool(backend: ClaudeBackend) -> Any:
    """Call _build_options() with ClaudeAgentOptions stubbed out and
    return the ``can_use_tool`` async callable that was passed to it."""
    captured: dict[str, Any] = {}

    class _FakeOptions:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    with patch("claude_agent_sdk.ClaudeAgentOptions", _FakeOptions):
        backend._build_options()

    return captured["can_use_tool"]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_set_permission_mode_callback_stores_cb() -> None:
    backend = _make_backend()
    assert backend._permission_mode_callback is None

    def cb(_mode: str) -> None:
        pass

    backend.set_permission_mode_callback(cb)

    assert backend._permission_mode_callback is cb


async def test_can_use_tool_calls_mode_cb_on_enter_plan_mode() -> None:
    """EnterPlanMode notifies the permission-mode callback with 'plan'
    and returns PermissionResultAllow without requiring any approval."""
    from claude_agent_sdk.types import PermissionResultAllow

    calls: list[str] = []
    backend = _make_backend()
    backend._permission_mode_callback = lambda m: calls.append(m)

    can_use_tool = _extract_can_use_tool(backend)
    result = await can_use_tool("EnterPlanMode", {}, None)

    assert isinstance(result, PermissionResultAllow)
    assert calls == ["plan"]


async def test_can_use_tool_calls_mode_cb_on_exit_plan_mode_approved() -> None:
    """ExitPlanMode notifies the permission-mode callback with 'default'
    and returns PermissionResultAllow when the plan-approval callback
    returns True."""
    from claude_agent_sdk.types import PermissionResultAllow

    calls: list[str] = []
    backend = _make_backend()
    backend._plan_approval_callback = lambda summary: True
    backend._permission_mode_callback = lambda m: calls.append(m)

    can_use_tool = _extract_can_use_tool(backend)
    result = await can_use_tool("ExitPlanMode", {"plan_summary": "do the thing"}, None)

    assert isinstance(result, PermissionResultAllow)
    assert calls == ["default"]


async def test_can_use_tool_does_not_call_mode_cb_on_exit_plan_mode_denied() -> None:
    """ExitPlanMode denied by plan-approval callback must NOT notify
    the permission-mode callback (mode stays 'plan')."""
    from claude_agent_sdk.types import PermissionResultDeny

    calls: list[str] = []
    backend = _make_backend()
    backend._plan_approval_callback = lambda summary: False
    backend._permission_mode_callback = lambda m: calls.append(m)

    can_use_tool = _extract_can_use_tool(backend)
    result = await can_use_tool("ExitPlanMode", {}, None)

    assert isinstance(result, PermissionResultDeny)
    assert calls == []  # mode stays "plan"
