import asyncio
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from obscura.cli.prompt import (
    PromptLayoutConfig,
    PromptHUDState,
    SlashCommandCompleter,
    _build_prompt_message_html,
    _render_menu_line,
    _render_model_status_line,
    bordered_prompt,
    create_prompt_session,
)
from obscura.cli.render import set_model_space_delta


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


def test_status_lane_never_wraps() -> None:
    width = 48
    hud = PromptHUDState(
        model_text="this is a very long model summary that must truncate",
        right_enabled=True,
        tasks_value="123456789",
        approvals_enabled=True,
        reasoning_enabled=True,
    )
    row = _render_model_status_line(width, hud)
    assert len(row) <= width
    assert "\n" not in row


def test_prompt_message_has_dedicated_input_lane() -> None:
    cfg = PromptLayoutConfig(model_hpad=1, input_hpad=1, model_vpad=0, input_vpad=0)
    msg = _build_prompt_message_html(60, "model: idle", cfg)
    lines = msg.splitlines()
    assert len(lines) == 2
    assert lines[0].startswith("<status-lane>")
    assert lines[1].startswith("<input-lane>")
    assert "│ " in lines[1]
    assert "<prompt>" not in lines[1]


def test_input_lane_contains_no_status_or_model_text() -> None:
    msg = _build_prompt_message_html(50, "model: thinking", PromptLayoutConfig())
    lane = msg.splitlines()[1]
    assert "model:" not in lane
    assert "thinking" not in lane
    assert "T:" not in lane


def test_menu_line_flex_render() -> None:
    hud = PromptHUDState(
        model_text="x",
        right_enabled=True,
        tasks_value="12",
        approvals_enabled=True,
        reasoning_enabled=False,
    )
    line = _render_menu_line(40, hud, PromptLayoutConfig(menu_hpad=1))
    assert "T:12" in line
    assert "A:on" in line
    assert "R:off" in line


def test_session_message_and_toolbar_are_separated() -> None:
    set_model_space_delta("thinking...")
    session = create_prompt_session(
        {"help": []},
        toolbar_text="ignored",
        hud_provider=lambda: {
            "right_enabled": True,
            "model_enabled": True,
            "menu_items": [("tasks", "3"), ("approvals", "on"), ("reasoning", "on")],
        },
    )
    msg = str(session.message())
    bar = str(session.bottom_toolbar())
    assert "status-lane" in msg
    assert "input-lane" in msg
    assert "T:3" not in msg
    assert "A:on" in bar
    assert "R:on" in bar
    assert "model:" not in bar


def test_model_delta_only_affects_model_status_lane() -> None:
    session = create_prompt_session(
        {"help": []},
        hud_provider=lambda: {
            "right_enabled": True,
            "model_enabled": True,
            "menu_items": [("tasks", "1"), ("approvals", "off"), ("reasoning", "on")],
        },
    )
    set_model_space_delta("phase: planning")
    msg = str(session.message())
    bar = str(session.bottom_toolbar())
    assert "phase: planning" in msg
    assert "phase: planning" not in bar
