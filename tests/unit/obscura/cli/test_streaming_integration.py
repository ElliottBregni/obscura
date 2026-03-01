import asyncio
from unittest.mock import MagicMock

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from prompt_toolkit import PromptSession

from obscura.cli.prompt import bordered_prompt
from obscura.cli.render import StreamRenderer, console
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
