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


# ── Tool call summary uses the shared summarizer ─────────────────────────


def test_tool_call_uses_shared_summarize_tool_call() -> None:
    """Should produce the same friendly summary the bordered REPL uses."""
    state = _make_state()
    renderer = TUIRenderer(state)
    renderer.handle(
        AgentEvent(
            kind=AgentEventKind.TOOL_CALL,
            tool_name="read_text_file",
            tool_input={"path": "/tmp/foo.py"},
            tool_use_id="tu_x",
        ),
    )
    assert state.transcript, "expected a TOOL_USE entry"
    entry = state.transcript[-1]
    assert entry.kind == TranscriptKind.TOOL_USE
    body = "".join(r.text for r in entry.runs)
    # ``read_text_file {"path": "/tmp/foo.py"}`` → ``Reading foo.py``.
    assert "Reading foo.py" in body, f"summary missing from runs: {body!r}"
    assert "read_text_file" in body


def test_tool_call_for_shell_includes_dollar_tag() -> None:
    """Shell-classified tools get a ``$`` tag prefix mirroring modern."""
    state = _make_state()
    renderer = TUIRenderer(state)
    renderer.handle(
        AgentEvent(
            kind=AgentEventKind.TOOL_CALL,
            tool_name="run_command",
            tool_input={"command": "ls -la"},
            tool_use_id="tu_y",
        ),
    )
    body = "".join(r.text for r in state.transcript[-1].runs)
    assert body.lstrip().startswith("$ ") or "$ " in body
    assert "ls -la" in body


def test_tool_call_for_mcp_includes_mcp_tag() -> None:
    """MCP shadow names get a ``MCP`` tag and ``server.tool`` summary."""
    state = _make_state()
    renderer = TUIRenderer(state)
    renderer.handle(
        AgentEvent(
            kind=AgentEventKind.TOOL_CALL,
            tool_name="mcp__github__list_repos",
            tool_input={"org": "anthropic"},
            tool_use_id="tu_z",
        ),
    )
    body = "".join(r.text for r in state.transcript[-1].runs)
    assert "MCP " in body, f"missing MCP tag: {body!r}"
    assert "github.list_repos" in body, f"missing server.tool summary: {body!r}"


def test_tool_call_live_preview_is_capped() -> None:
    """A long input should never become a > 50-char ``state.live.preview``."""
    state = _make_state()
    renderer = TUIRenderer(state)
    long_input = "x" * 500
    renderer.handle(
        AgentEvent(
            kind=AgentEventKind.TOOL_CALL,
            tool_name="run_command",
            tool_input={"command": long_input},
            tool_use_id="tu_l",
        ),
    )
    assert len(state.live.preview) <= 50


# ── Tool result formatting ───────────────────────────────────────────────


def test_tool_result_json_dict_extracts_stdout() -> None:
    """``{"ok": true, "stdout": "hello\\n"}`` should render as ``hello``."""
    state = _make_state()
    renderer = TUIRenderer(state)
    renderer.handle(
        AgentEvent(
            kind=AgentEventKind.TOOL_RESULT,
            tool_name="run_command",
            tool_use_id="tu_1",
            tool_result='{"ok": true, "stdout": "hello\\n", "stderr": "", "exit_code": 0}',
        ),
    )
    body = "".join(r.text for r in state.transcript[-1].runs)
    # Body should surface the readable stdout, not the JSON envelope.
    assert "hello" in body
    assert "{\"ok\":" not in body
    # Success glyph leads the body.
    assert body.lstrip().startswith("✓")


def test_tool_result_json_failure_marks_error() -> None:
    """A non-zero ``exit_code`` flips the entry to error severity."""
    state = _make_state()
    renderer = TUIRenderer(state)
    renderer.handle(
        AgentEvent(
            kind=AgentEventKind.TOOL_RESULT,
            tool_name="run_command",
            tool_use_id="tu_2",
            tool_result='{"ok": false, "stdout": "", "stderr": "command not found", "exit_code": 127}',
        ),
    )
    body = "".join(r.text for r in state.transcript[-1].runs)
    assert "command not found" in body
    assert body.lstrip().startswith("✗")


def test_tool_result_multiline_splits_into_indented_lines() -> None:
    """Multi-line plain text result should split with continuation indent."""
    state = _make_state()
    renderer = TUIRenderer(state)
    raw = "line one\nline two\nline three"
    renderer.handle(
        AgentEvent(
            kind=AgentEventKind.TOOL_RESULT,
            tool_name="read_text_file",
            tool_use_id="tu_3",
            tool_result=raw,
        ),
    )
    body = "".join(r.text for r in state.transcript[-1].runs)
    assert "line one" in body
    assert "line two" in body
    assert "line three" in body
    # Continuation rows are indented under the glyph.
    assert "\n    line two" in body


def test_tool_result_strips_ansi_escapes() -> None:
    """Shell tool results with colour codes should not leak escapes."""
    state = _make_state()
    renderer = TUIRenderer(state)
    raw_with_ansi = "\x1b[31mred error\x1b[0m"
    renderer.handle(
        AgentEvent(
            kind=AgentEventKind.TOOL_RESULT,
            tool_name="run_command",
            tool_use_id="tu_4",
            tool_result=raw_with_ansi,
        ),
    )
    body = "".join(r.text for r in state.transcript[-1].runs)
    assert "\x1b[" not in body
    assert "red error" in body


def test_tool_result_long_output_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Very tall results should be capped with a ``… (N more lines)`` hint."""
    monkeypatch.setenv("OBSCURA_TOOL_OUTPUT_MAX_LINES", "5")
    state = _make_state()
    renderer = TUIRenderer(state)
    raw = "\n".join(f"line {i}" for i in range(50))
    renderer.handle(
        AgentEvent(
            kind=AgentEventKind.TOOL_RESULT,
            tool_name="read_text_file",
            tool_use_id="tu_5",
            tool_result=raw,
        ),
    )
    body = "".join(r.text for r in state.transcript[-1].runs)
    # Cap means we keep only 5 of the 50 lines.
    assert "line 0" in body
    assert "line 4" in body
    assert "line 49" not in body
    assert "more lines" in body


def test_tool_result_empty_renders_placeholder() -> None:
    """Empty / whitespace-only results should show ``(empty result)``."""
    state = _make_state()
    renderer = TUIRenderer(state)
    renderer.handle(
        AgentEvent(
            kind=AgentEventKind.TOOL_RESULT,
            tool_name="read_text_file",
            tool_use_id="tu_6",
            tool_result="   \n   ",
        ),
    )
    body = "".join(r.text for r in state.transcript[-1].runs)
    assert "(empty result)" in body
