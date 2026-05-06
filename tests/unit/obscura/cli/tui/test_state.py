"""Tests for ``obscura.cli.tui.state`` Pydantic models and mutators."""

from __future__ import annotations

import time

import pytest

from obscura.cli.renderer.channels import Severity
from obscura.cli.tui.state import (
    HUDState,
    LiveRegionKind,
    LiveRegionState,
    NotificationItem,
    ToolApprovalRequest,
    TranscriptEntry,
    TranscriptKind,
    TUIMode,
    TUIState,
)

pytestmark = pytest.mark.unit


def _make_state() -> TUIState:
    """Construct a TUIState with a minimal HUDState."""
    hud = HUDState(
        backend="copilot",
        model="gpt-4",
        session_id="abcd1234efgh",
    )
    return TUIState(hud=hud)


def test_tui_state_defaults_zero_everywhere() -> None:
    state = _make_state()

    assert state.transcript == []
    assert state.notifications == []
    assert state.banner is None
    assert state.pending_approval is None
    assert state.live.kind == LiveRegionKind.IDLE
    assert state.show_agent_panel is True
    assert state.show_thinking is True
    # HUD defaults
    assert state.hud.mode == TUIMode.CHAT
    assert state.hud.ctx_pct == 0
    assert state.hud.ctx_tokens == 0
    assert state.hud.ctx_window == 0
    assert state.hud.task_count == 0
    assert state.hud.running_agents == []
    assert state.hud.is_streaming is False


def test_append_transcript_caps_at_5000() -> None:
    state = _make_state()
    for _ in range(5100):
        state.append_transcript(TranscriptEntry(kind=TranscriptKind.SYSTEM))
    assert len(state.transcript) == 5000


def test_push_notification_replaces_by_key() -> None:
    state = _make_state()
    n1 = NotificationItem(title="task: 1/3", key="task-progress")
    n2 = NotificationItem(title="task: 2/3", key="task-progress")
    state.push_notification(n1)
    state.push_notification(n2)
    assert len(state.notifications) == 1
    assert state.notifications[0].title == "task: 2/3"


def test_push_notification_without_key_stacks() -> None:
    state = _make_state()
    state.push_notification(NotificationItem(title="a"))
    state.push_notification(NotificationItem(title="b"))
    state.push_notification(NotificationItem(title="c"))
    assert [n.title for n in state.notifications] == ["a", "b", "c"]


def test_prune_notifications_drops_expired_and_returns_count() -> None:
    state = _make_state()
    now = time.monotonic()
    fresh = NotificationItem(title="fresh", ttl_seconds=1000.0)
    expired_a = NotificationItem(
        title="old-a",
        ttl_seconds=1.0,
        created_at_monotonic=now - 100.0,
    )
    expired_b = NotificationItem(
        title="old-b",
        ttl_seconds=1.0,
        created_at_monotonic=now - 200.0,
    )
    state.notifications = [fresh, expired_a, expired_b]

    removed = state.prune_notifications()
    assert removed == 2
    assert len(state.notifications) == 1
    assert state.notifications[0].title == "fresh"


def test_open_and_close_approval_toggles_mode() -> None:
    state = _make_state()
    req = ToolApprovalRequest(
        tool_use_id="t1",
        tool_name="bash",
        tool_input={"command": "ls"},
    )
    state.open_approval(req)
    assert state.pending_approval is req
    assert state.hud.mode == TUIMode.APPROVAL

    state.close_approval()
    assert state.pending_approval is None
    assert state.hud.mode == TUIMode.CHAT


def test_live_region_state_elapsed_zero_when_idle() -> None:
    live = LiveRegionState(kind=LiveRegionKind.IDLE)
    assert live.elapsed_s == 0.0


def test_live_region_state_elapsed_positive_when_active() -> None:
    live = LiveRegionState(kind=LiveRegionKind.STREAMING)
    # Force the clock back so elapsed_s is unambiguously > 0
    live.started_at_monotonic = time.monotonic() - 1.5
    assert live.elapsed_s > 0.0


def test_severity_enum_round_trip_through_notification_item() -> None:
    item = NotificationItem(title="x", severity=Severity.WARN)
    assert item.severity is Severity.WARN
