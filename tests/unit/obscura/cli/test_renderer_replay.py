from __future__ import annotations

import json
from unittest.mock import MagicMock

from obscura.cli.render import StreamRenderer, output
from obscura.core.types import AgentEvent, AgentEventKind


def test_renderer_replay_message_flow_captures_hidden_deltas(tmp_path) -> None:
    output.configure_session_log_path(tmp_path)
    output.set_log_level("medium")
    output._buffer.clear()

    status = MagicMock()
    renderer = StreamRenderer(external_status=status)
    events = [
        AgentEvent(kind=AgentEventKind.TURN_START),
        AgentEvent(kind=AgentEventKind.THINKING_DELTA, text="planning"),
        AgentEvent(kind=AgentEventKind.THINKING_DELTA, text=" edits"),
        AgentEvent(kind=AgentEventKind.TOOL_CALL, tool_name="read_file", tool_input={"path": "x.py"}),
        AgentEvent(kind=AgentEventKind.TOOL_RESULT, tool_result="ok", is_error=False),
        AgentEvent(kind=AgentEventKind.TEXT_DELTA, text="Applied changes."),
        AgentEvent(kind=AgentEventKind.TURN_COMPLETE),
    ]
    for ev in events:
        renderer.handle(ev)
    renderer.finish()

    # Hidden deltas are persisted even though token deltas are not mirrored to the prompt.
    hidden = tmp_path / "hidden_deltas.log"
    assert hidden.exists()
    rows = [json.loads(ln) for ln in hidden.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(rows) == 2
    assert rows[0]["kind"] == "REASONING_DELTA"
    assert rows[0]["text"] == "planning"

    # Medium mode suppresses noisy REASONING_DELTA entries from internal buffer.
    assert not any(line.startswith("REASONING_DELTA") for line in output.get_buffer())
    assert status.update.call_count >= 1
