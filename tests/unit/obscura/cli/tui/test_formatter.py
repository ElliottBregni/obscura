"""Tests for the pure event-to-state translator in ``obscura.cli.tui.formatter``."""

from __future__ import annotations

import pytest

from obscura.cli.renderer.channels import Notification as ChannelNotification
from obscura.cli.renderer.channels import Severity, StatusEvent
from obscura.cli.tui.formatter import (
    format_notification,
    format_slash_output,
    format_status_event,
    format_transcript_event,
    format_user_prompt,
)
from obscura.cli.tui.state import LiveRegionKind, TranscriptKind
from obscura.core.enums.agent import AgentEventKind
from obscura.core.types import AgentEvent

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# format_user_prompt
# ---------------------------------------------------------------------------


def test_format_user_prompt_marks_user_kind_with_styled_run() -> None:
    entry = format_user_prompt("hello")
    assert entry.kind == TranscriptKind.USER
    assert len(entry.runs) == 1
    run = entry.runs[0]
    assert run.text == "hello"
    # OK_HEX is a green hue from the theme — check the literal token forms.
    assert "fg:" in run.style
    assert "bold" in run.style


def test_format_user_prompt_empty_input_yields_no_runs() -> None:
    entry = format_user_prompt("")
    assert entry.kind == TranscriptKind.USER
    assert entry.runs == []
    assert entry.metadata == {"raw_text": ""}


# ---------------------------------------------------------------------------
# format_slash_output — strips ANSI
# ---------------------------------------------------------------------------


def test_format_slash_output_strips_ansi_csi_sequences() -> None:
    raw = "\x1b[32mok\x1b[0m text"
    entry = format_slash_output(raw)
    assert entry.kind == TranscriptKind.SLASH_OUTPUT
    # No raw escape characters remain in any run.
    for run in entry.runs:
        assert "\x1b" not in run.text
    full = "".join(r.text for r in entry.runs)
    assert "ok text" in full
    # raw is preserved in metadata for replay/copy.
    assert entry.metadata["raw_text"] == raw


# ---------------------------------------------------------------------------
# format_transcript_event — per-kind dispatch
# ---------------------------------------------------------------------------


def test_format_transcript_event_text_delta() -> None:
    ev = AgentEvent(kind=AgentEventKind.TEXT_DELTA, text="hello world")
    entry = format_transcript_event(ev)
    assert entry.kind == TranscriptKind.ASSISTANT
    assert "".join(r.text for r in entry.runs) == "hello world"


def test_format_transcript_event_thinking_delta() -> None:
    ev = AgentEvent(kind=AgentEventKind.THINKING_DELTA, text="reasoning step")
    entry = format_transcript_event(ev)
    assert entry.kind == TranscriptKind.THINKING
    full = "".join(r.text for r in entry.runs)
    # The body text appears somewhere in the rendered runs.
    assert "reasoning step" in full
    # And there's a coloured left-bar prefix.
    assert "▎" in full


def test_format_transcript_event_tool_call_with_path_arg() -> None:
    ev = AgentEvent(
        kind=AgentEventKind.TOOL_CALL,
        tool_name="read_text_file",
        tool_input={"path": "/tmp/x.py"},
        tool_use_id="tu1",
    )
    entry = format_transcript_event(ev)
    assert entry.kind == TranscriptKind.TOOL_USE
    full = "".join(r.text for r in entry.runs)
    assert "read_text_file" in full
    assert "/tmp/x.py" in full
    # No JSON quotes in the rendered detail line.
    assert "{" not in full
    assert entry.metadata["tool_name"] == "read_text_file"
    assert entry.metadata["tool_use_id"] == "tu1"
    assert entry.metadata["tool_input"] == {"path": "/tmp/x.py"}


def test_format_transcript_event_tool_result_success_short() -> None:
    ev = AgentEvent(
        kind=AgentEventKind.TOOL_RESULT,
        tool_name="bash",
        tool_use_id="tu2",
        tool_result="exit 0",
        is_error=False,
    )
    entry = format_transcript_event(ev)
    assert entry.kind == TranscriptKind.TOOL_RESULT
    full = "".join(r.text for r in entry.runs)
    assert "exit 0" in full
    assert "✗" not in full  # success — no error glyph
    assert entry.parent_id == "tu2"
    assert entry.metadata["is_error"] is False


def test_format_transcript_event_tool_result_error_long_keeps_full_capped() -> None:
    long_text = "BOOM\n" + ("x" * 5000)
    ev = AgentEvent(
        kind=AgentEventKind.TOOL_RESULT,
        tool_name="bash",
        tool_use_id="tu3",
        tool_result=long_text,
        is_error=True,
    )
    entry = format_transcript_event(ev)
    assert entry.kind == TranscriptKind.TOOL_RESULT
    full = "".join(r.text for r in entry.runs)
    # Error glyph is rendered.
    assert "✗" in full
    # Full text isn't fully reproduced — it's capped to ≤2000 chars in the
    # "result" run (plus the prefix run).
    body_runs = [r for r in entry.runs if "✗" not in r.text]
    body_text = "".join(r.text for r in body_runs)
    assert len(body_text) <= 2000 + 16  # cap + small prefix slack
    assert entry.metadata["is_error"] is True


def test_format_transcript_event_error_kind() -> None:
    ev = AgentEvent(kind=AgentEventKind.ERROR, text="something went wrong")
    entry = format_transcript_event(ev)
    assert entry.kind == TranscriptKind.ERROR
    full = "".join(r.text for r in entry.runs)
    assert "something went wrong" in full


# ---------------------------------------------------------------------------
# format_status_event
# ---------------------------------------------------------------------------


def test_format_status_event_inactive_returns_idle() -> None:
    live = format_status_event(StatusEvent(active=False))
    assert live.kind == LiveRegionKind.IDLE


def test_format_status_event_active_running_kind() -> None:
    live = format_status_event(
        StatusEvent(text="running edit_text_file", preview="x.py", active=True)
    )
    assert live.kind == LiveRegionKind.TOOL_RUNNING
    assert live.label == "running edit_text_file"
    assert live.preview == "x.py"


def test_format_status_event_calling_or_streaming_kind() -> None:
    live = format_status_event(StatusEvent(text="streaming", active=True))
    assert live.kind == LiveRegionKind.STREAMING


# ---------------------------------------------------------------------------
# format_notification
# ---------------------------------------------------------------------------


def test_format_notification_preserves_fields() -> None:
    cn = ChannelNotification(
        title="task: 1/3",
        body="indexing repo",
        severity=Severity.WARN,
        source="kairos",
        ttl_seconds=2.5,
        key="kairos-progress",
    )
    item = format_notification(cn)
    assert item.title == "task: 1/3"
    assert item.body == "indexing repo"
    assert item.severity is Severity.WARN
    assert item.source == "kairos"
    assert item.ttl_seconds == 2.5
    assert item.key == "kairos-progress"
