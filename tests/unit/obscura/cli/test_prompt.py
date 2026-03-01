import asyncio
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from obscura.cli.prompt import create_prompt_session, bordered_prompt, SlashCommandCompleter


def test_slash_completer_basic():
    completions = {"help": ["topics"], "backend": ["copilot", "claude"]}
    completer = SlashCommandCompleter(completions)
    # Simulate a document requesting completions for "/he"
    from prompt_toolkit.document import Document

    docs = Document("/he", cursor_position=len("/he"))
    # collect completions
    results = list(completer.get_completions(docs, None))
    assert any(c.text == "/help" for c in results)


async def _run_bordered_input(text: str):
    from prompt_toolkit import PromptSession
    # create a pipe-backed PromptSession for testing
    with create_pipe_input() as pipe:
        session = PromptSession(message="\u276f ", input=pipe, output=DummyOutput())
        # feed input and run bordered_prompt
        pipe.send_text(text + "\n")
        result = await bordered_prompt(session)
        return result


def test_bordered_prompt_event_loop():
    # Basic smoke test: ensure bordered_prompt returns stripped input
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        async def coro():
            # use run_until_complete with a short timeout
            return await _run_bordered_input("hello")

        res = loop.run_until_complete(coro())
        assert res == "hello"
    finally:
        loop.close()
