"""Tests for ``obscura.cli.tui.renderer.TUIRenderer`` event-stream behavior."""

from __future__ import annotations

import pytest

from obscura.cli.tui.renderer import TUIRenderer
from obscura.cli.tui.state import (
    HUDState,
    TranscriptKind,
    TUIState,
)
from obscura.core.enums.agent import AgentEventKind
from obscura.core.types import AgentEvent

pytestmark = pytest.mark.unit


def _make_state() -> TUIState:
    hud = HUDState(backend="copilot", model="gpt-4", session_id="abcd1234efgh")
    return TUIState(hud=hud)


def test_renderer_full_turn_yields_four_transcript_entries() -> None:
    state = _make_state()
    invalidations: list[int] = []
    renderer = TUIRenderer(state, invalidate=lambda: invalidations.append(1))

    events = [
        AgentEvent(kind=AgentEventKind.TURN_START),
        AgentEvent(kind=AgentEventKind.THINKING_DELTA, text="step 1\n"),
        AgentEvent(kind=AgentEventKind.THINKING_DELTA, text="step 2"),
        AgentEvent(kind=AgentEventKind.TEXT_DELTA, text="Hello "),
        AgentEvent(kind=AgentEventKind.TEXT_DELTA, text="world"),
        AgentEvent(
            kind=AgentEventKind.TOOL_CALL,
            tool_name="bash",
            tool_input={"command": "echo hi"},
            tool_use_id="tu1",
        ),
        AgentEvent(
            kind=AgentEventKind.TOOL_RESULT,
            tool_name="bash",
            tool_use_id="tu1",
            tool_result="hi",
        ),
        AgentEvent(kind=AgentEventKind.TURN_COMPLETE),
        AgentEvent(kind=AgentEventKind.AGENT_DONE),
    ]
    for ev in events:
        renderer.handle(ev)

    kinds = [e.kind for e in state.transcript]
    # Order: thinking flushed when TEXT_DELTA arrives; assistant flushed
    # when TOOL_CALL arrives; tool_use, tool_result; subsequent
    # TURN_COMPLETE/AGENT_DONE flush no further text.
    assert kinds == [
        TranscriptKind.THINKING,
        TranscriptKind.ASSISTANT,
        TranscriptKind.TOOL_USE,
        TranscriptKind.TOOL_RESULT,
    ]
    # Invalidate is called once per event.
    assert len(invalidations) == len(events)


def test_renderer_get_accumulated_text_concatenates_text_deltas() -> None:
    state = _make_state()
    renderer = TUIRenderer(state)
    renderer.handle(AgentEvent(kind=AgentEventKind.TEXT_DELTA, text="Hello "))
    renderer.handle(AgentEvent(kind=AgentEventKind.TEXT_DELTA, text="world"))
    # Flush via TURN_COMPLETE so _text_buf is cleared; _all_text remains.
    # (Mirrors the StreamRenderer contract — get_accumulated_text is meant
    # to be called post-flush; pre-flush the unflushed runtime double-counts
    # because _all_text is also appended on every delta.)
    renderer.handle(AgentEvent(kind=AgentEventKind.TURN_COMPLETE))
    assert renderer.get_accumulated_text() == "Hello world"


def test_renderer_last_thinking_block_is_joined() -> None:
    state = _make_state()
    renderer = TUIRenderer(state)
    renderer.handle(AgentEvent(kind=AgentEventKind.THINKING_DELTA, text="ab"))
    renderer.handle(AgentEvent(kind=AgentEventKind.THINKING_DELTA, text="cd"))
    # Force a flush by handling a TEXT_DELTA.
    renderer.handle(AgentEvent(kind=AgentEventKind.TEXT_DELTA, text="x"))
    assert renderer.get_last_thinking() == "abcd"


def test_renderer_finish_is_idempotent() -> None:
    state = _make_state()
    renderer = TUIRenderer(state)
    renderer.handle(AgentEvent(kind=AgentEventKind.TEXT_DELTA, text="hi"))
    renderer.finish()
    snapshot_after_first = list(state.transcript)
    # Second finish should not raise or add new entries.
    renderer.finish()
    assert state.transcript == snapshot_after_first
    # The single ASSISTANT entry was flushed once.
    assert [e.kind for e in state.transcript] == [TranscriptKind.ASSISTANT]


def test_renderer_error_mid_stream_flushes_pending_text_and_thinking() -> None:
    state = _make_state()
    renderer = TUIRenderer(state)
    renderer.handle(AgentEvent(kind=AgentEventKind.THINKING_DELTA, text="thinking..."))
    renderer.handle(AgentEvent(kind=AgentEventKind.TEXT_DELTA, text="partial assist"))
    renderer.handle(
        AgentEvent(kind=AgentEventKind.ERROR, text="stream blew up"),
    )

    kinds = [e.kind for e in state.transcript]
    # Thinking flushed first, then assistant, then error.
    assert kinds == [
        TranscriptKind.THINKING,
        TranscriptKind.ASSISTANT,
        TranscriptKind.ERROR,
    ]
    err = state.transcript[-1]
    assert "stream blew up" in "".join(r.text for r in err.runs)
