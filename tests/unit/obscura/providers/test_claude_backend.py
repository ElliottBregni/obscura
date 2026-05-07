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


async def test_can_use_tool_falls_back_to_session_plan_approval_callback() -> None:
    """When ClaudeBackend._plan_approval_callback is None (REPL path),
    can_use_tool falls back to Session.plan_approval_callback so the REPL
    still gets an approval gate instead of auto-approving."""
    import obscura.tools.system._session as _session_mod
    from claude_agent_sdk.types import PermissionResultAllow

    calls: list[str] = []
    original_cb = _session_mod.Session.plan_approval_callback

    try:
        _session_mod.Session.plan_approval_callback = lambda summary: (  # type: ignore[assignment]
            calls.append(summary) or True
        )
        backend = _make_backend()
        # _plan_approval_callback stays None — simulates REPL path
        assert backend._plan_approval_callback is None

        can_use_tool = _extract_can_use_tool(backend)
        result = await can_use_tool("ExitPlanMode", {"plan_summary": "ship it"}, None)

        assert isinstance(result, PermissionResultAllow)
        assert calls == ["ship it"]
    finally:
        _session_mod.Session.plan_approval_callback = original_cb


async def test_can_use_tool_auto_allows_when_no_callbacks_registered() -> None:
    """ExitPlanMode is allowed without any dialog when neither
    ClaudeBackend._plan_approval_callback nor Session.plan_approval_callback
    is set (e.g., in scripted / non-interactive contexts)."""
    import obscura.tools.system._session as _session_mod
    from claude_agent_sdk.types import PermissionResultAllow

    original_cb = _session_mod.Session.plan_approval_callback
    try:
        _session_mod.Session.plan_approval_callback = None  # type: ignore[assignment]
        backend = _make_backend()

        can_use_tool = _extract_can_use_tool(backend)
        result = await can_use_tool("ExitPlanMode", {}, None)

        assert isinstance(result, PermissionResultAllow)
    finally:
        _session_mod.Session.plan_approval_callback = original_cb
