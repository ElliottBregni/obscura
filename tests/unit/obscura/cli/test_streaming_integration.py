import asyncio
import json
from unittest.mock import MagicMock

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from prompt_toolkit import PromptSession

from obscura.cli.prompt import bordered_prompt
from obscura.cli.render import OutputManager, StreamRenderer, console
from obscura.core.types import AgentEvent, AgentEventKind


@pytest.mark.asyncio
async def test_streaming_updates_during_prompt() -> None:
    """Ensure StreamRenderer updates external status while a prompt is active and doesn't error."""
    with create_pipe_input() as pipe:
        session = PromptSession(message="\u276f ", input=pipe, output=DummyOutput())

        # start prompt in background (it will await input)
        prompt_task = asyncio.create_task(bordered_prompt(session))

        # allow the prompt to start and enter patched stdout
        await asyncio.sleep(0.05)

        mock_status = MagicMock()
        renderer = StreamRenderer(external_status=mock_status)

        # send a thinking delta then a text delta and a tool call/result
        renderer.handle(AgentEvent(kind=AgentEventKind.THINKING_DELTA, text="thinking..."))
        renderer.handle(AgentEvent(kind=AgentEventKind.TEXT_DELTA, text="hello world"))
        renderer.handle(AgentEvent(kind=AgentEventKind.TOOL_CALL, tool_name="echo", tool_input={"q": "1"}))
        renderer.handle(AgentEvent(kind=AgentEventKind.TOOL_RESULT, tool_result="ok", is_error=False))

        # allow background updates to run
        await asyncio.sleep(0.05)

        # ensure external status.update was called at least once
        assert mock_status.update.call_count >= 1
        rendered_updates = [str(c.args[0]) for c in mock_status.update.call_args_list if c.args]
        assert any("thinking" in u for u in rendered_updates)

        # finish prompt so test can exit
        pipe.send_text("done\n")
        res = await prompt_task
        assert res == "done"


def test_non_tty_fallback(monkeypatch) -> None:
    """When external status.update raises, StreamRenderer should fallback to printing via console."""

    calls = []

    def fake_print(*args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setattr(console, "print", fake_print)

    class BadStatus:
        def update(self, *args, **kwargs):
            raise AttributeError("update not supported")

    renderer = StreamRenderer(external_status=BadStatus())

    # thinking delta should attempt update and on exception fall back to console.print
    renderer.handle(AgentEvent(kind=AgentEventKind.THINKING_DELTA, text="i am thinking"))
    renderer.finish()

    # Expect at least one console.print fallback call
    assert len(calls) >= 1


def test_thinking_status_in_external_status_line() -> None:
    """When model status is present in raw event, include it in prompt status line."""
    mock_status = MagicMock()
    renderer = StreamRenderer(external_status=mock_status)
    renderer.handle(
        AgentEvent(
            kind=AgentEventKind.THINKING_DELTA,
            text="analyzing",
            raw={"status": "planning edits"},
        )
    )
    updates = [str(c.args[0]) for c in mock_status.update.call_args_list if c.args]
    assert any("planning edits" in u for u in updates)


def test_hidden_reasoning_deltas_are_persisted(tmp_path) -> None:
    out = OutputManager(env="cli", verbose_internals=False, log_level="low")
    out.configure_session_log_path(tmp_path)
    out.capture_hidden_delta("REASONING_DELTA", "alpha", status="planning")
    out.capture_hidden_delta("REASONING_DELTA", "beta", status="")

    p = tmp_path / "hidden_deltas.log"
    assert p.exists()
    lines = [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 2
    row0 = json.loads(lines[0])
    assert row0["kind"] == "REASONING_DELTA"
    assert row0["status"] == "planning"
    assert row0["text"] == "alpha"


def test_reasoning_normalized_to_clean_paragraph() -> None:
    raw = "  first line\nsecond line  \n\n third line\n\n\nfourth  line "
    normalized = StreamRenderer._normalize_reasoning_text(raw)
    assert normalized == "first line second line\n\nthird line\n\nfourth line"


def test_reasoning_preview_uses_jitter(monkeypatch) -> None:
    monkeypatch.setenv("OBSCURA_REASONING_JITTER_MS", "200")
    mock_status = MagicMock()
    renderer = StreamRenderer(external_status=mock_status)

    ticks = iter([0.00, 0.05, 0.10, 0.35])
    monkeypatch.setattr("obscura.cli.render.time.monotonic", lambda: next(ticks))

    renderer.handle(AgentEvent(kind=AgentEventKind.THINKING_DELTA, text="a"))
    renderer.handle(AgentEvent(kind=AgentEventKind.THINKING_DELTA, text="b"))
    renderer.handle(AgentEvent(kind=AgentEventKind.THINKING_DELTA, text="c"))
    renderer.handle(AgentEvent(kind=AgentEventKind.THINKING_DELTA, text="d"))

    # First and fourth deltas should update; middle deltas are suppressed by jitter.
    assert mock_status.update.call_count == 2


def test_flush_thinking_does_not_emit_reasoning_preview_text_to_status() -> None:
    mock_status = MagicMock()
    renderer = StreamRenderer(external_status=mock_status)
    renderer.handle(AgentEvent(kind=AgentEventKind.THINKING_DELTA, text="deep plan steps"))
    # Trigger thinking flush
    renderer.handle(AgentEvent(kind=AgentEventKind.TEXT_DELTA, text="final answer"))
    updates = [str(c.args[0]) for c in mock_status.update.call_args_list if c.args]
    # Status line should stay compact (no echoed "[thinking] ..." preview payload).
    assert not any("[thinking]" in u for u in updates)
