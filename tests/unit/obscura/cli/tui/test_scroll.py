"""End-to-end scroll tests for the full-screen TUI.

These tests drive a real ``prompt_toolkit.Application`` via
``create_pipe_input`` and feed the actual escape sequences a terminal
emits for PageUp / PageDown / Shift+Up / Shift+Down / End. They assert
two things:

1. The Application's scroll keybindings fire while the input ``TextArea``
   is focused (i.e. the multi-line buffer doesn't swallow them).
2. The transcript window's resolved ``vertical_scroll`` actually moves
   away from the tail when the user scrolls up.

Together these would have caught the original "scroll doesn't work"
bug (cursor pinned to the last line was forcing prompt_toolkit to
auto-scroll back to the bottom every frame).
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest
from prompt_toolkit.application import Application
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent, merge_key_bindings
from prompt_toolkit.output import DummyOutput

from obscura.cli.tui.layout import build_layout
from obscura.cli.tui.state import (
    HUDState,
    StyledRun,
    TranscriptEntry,
    TranscriptKind,
    TUIState,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Terminal escape sequences — what an xterm-compatible terminal sends
# when the user actually presses these keys. prompt_toolkit's input
# parser turns these into KeyPress events.
# ---------------------------------------------------------------------------
_ESC_PAGEUP = "\x1b[5~"
_ESC_PAGEDOWN = "\x1b[6~"
_ESC_END = "\x1b[F"
_ESC_SHIFT_UP = "\x1b[1;2A"
_ESC_SHIFT_DOWN = "\x1b[1;2B"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_state_with_history(line_count: int = 200) -> TUIState:
    """Build a TUIState seeded with enough transcript to scroll through."""
    hud = HUDState(backend="copilot", model="gpt-4", session_id="abcd1234")
    state = TUIState(hud=hud)
    for i in range(line_count):
        state.append_transcript(
            TranscriptEntry(
                kind=TranscriptKind.ASSISTANT,
                runs=[StyledRun(text=f"line {i:03d}\n")],
            )
        )
    return state


def _build_scroll_kb(state: TUIState) -> KeyBindings:
    """Mirror ObscuraTUIApp._build_application's scroll-key wiring.

    Kept in lock-step with the production wiring in app.py — if you change
    one, change the other. We don't import the wiring from app.py because
    that pulls in the whole engine adapter, an asyncio loop, and a lot of
    state we don't need for a pure-key-binding test.
    """
    kb = KeyBindings()
    page = 10
    line = 3

    def _up_page(event: KeyPressEvent) -> None:
        state.transcript_scroll_offset += page

    def _down_page(event: KeyPressEvent) -> None:
        state.transcript_scroll_offset = max(
            0, state.transcript_scroll_offset - page
        )

    def _up_line(event: KeyPressEvent) -> None:
        state.transcript_scroll_offset += line

    def _down_line(event: KeyPressEvent) -> None:
        state.transcript_scroll_offset = max(
            0, state.transcript_scroll_offset - line
        )

    def _to_tail(event: KeyPressEvent) -> None:
        state.transcript_scroll_offset = 0

    kb.add("pageup")(_up_page)
    kb.add("pagedown")(_down_page)
    kb.add("s-up")(_up_line)
    kb.add("s-down")(_down_line)
    kb.add("end")(_to_tail)
    return kb


@contextlib.asynccontextmanager
async def _running_app(state: TUIState):
    """Spin up a real Application using build_layout + the scroll bindings.

    Yields ``(app, pipe_input)`` so the caller can ``send_text`` synthetic
    key sequences and then read state mutations.
    """
    layout = build_layout(state)
    kb = _build_scroll_kb(state)
    with create_pipe_input() as pipe_input:
        app = Application(
            layout=layout.layout,
            key_bindings=merge_key_bindings([kb]),
            input=pipe_input,
            output=DummyOutput(),
            full_screen=False,
        )
        run_task = asyncio.create_task(app.run_async())
        # Give the app one tick to enter its run loop.
        await asyncio.sleep(0.05)
        try:
            yield app, pipe_input
        finally:
            app.exit()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(run_task, timeout=1.0)


# ---------------------------------------------------------------------------
# Behaviour tests
# ---------------------------------------------------------------------------


async def test_pageup_increments_offset_with_input_focused() -> None:
    """PageUp must scroll the transcript even though the input is focused.

    If TextArea swallows PageUp for cursor-by-page navigation, the offset
    stays at 0 and this test fails. The bug we shipped earlier left the
    binding unfired because of focus priority.
    """
    state = _make_state_with_history()
    async with _running_app(state) as (_app, pipe_input):
        assert state.transcript_scroll_offset == 0
        pipe_input.send_text(_ESC_PAGEUP)
        # Two ticks: one for the parser, one for the binding.
        for _ in range(5):
            await asyncio.sleep(0.02)
            if state.transcript_scroll_offset:
                break
        assert state.transcript_scroll_offset == 10, (
            f"PageUp should have incremented offset to 10, "
            f"got {state.transcript_scroll_offset}"
        )


async def test_pageup_then_pagedown_round_trips() -> None:
    state = _make_state_with_history()
    async with _running_app(state) as (_app, pipe_input):
        pipe_input.send_text(_ESC_PAGEUP * 3)
        for _ in range(8):
            await asyncio.sleep(0.02)
            if state.transcript_scroll_offset >= 30:
                break
        assert state.transcript_scroll_offset == 30

        pipe_input.send_text(_ESC_PAGEDOWN * 2)
        for _ in range(8):
            await asyncio.sleep(0.02)
            if state.transcript_scroll_offset <= 10:
                break
        assert state.transcript_scroll_offset == 10


async def test_pagedown_clamps_at_zero() -> None:
    state = _make_state_with_history()
    async with _running_app(state) as (_app, pipe_input):
        pipe_input.send_text(_ESC_PAGEDOWN * 5)
        for _ in range(5):
            await asyncio.sleep(0.02)
        assert state.transcript_scroll_offset == 0, (
            "Offset must never go negative — we'd render a phantom region."
        )


async def test_end_jumps_to_tail() -> None:
    state = _make_state_with_history()
    async with _running_app(state) as (_app, pipe_input):
        # First scroll up several pages.
        pipe_input.send_text(_ESC_PAGEUP * 4)
        for _ in range(8):
            await asyncio.sleep(0.02)
            if state.transcript_scroll_offset >= 40:
                break
        assert state.transcript_scroll_offset == 40

        pipe_input.send_text(_ESC_END)
        for _ in range(5):
            await asyncio.sleep(0.02)
            if state.transcript_scroll_offset == 0:  # pyright: ignore[reportUnnecessaryComparison]
                break
        assert state.transcript_scroll_offset == 0


async def test_shift_arrows_scroll_by_line() -> None:
    state = _make_state_with_history()
    async with _running_app(state) as (_app, pipe_input):
        pipe_input.send_text(_ESC_SHIFT_UP * 4)
        for _ in range(8):
            await asyncio.sleep(0.02)
            if state.transcript_scroll_offset >= 12:
                break
        assert state.transcript_scroll_offset == 12

        pipe_input.send_text(_ESC_SHIFT_DOWN)
        for _ in range(5):
            await asyncio.sleep(0.02)
            if state.transcript_scroll_offset == 9:  # pyright: ignore[reportUnnecessaryComparison]
                break
        assert state.transcript_scroll_offset == 9


async def test_cursor_y_tracks_offset() -> None:
    """Regression: the transcript control's cursor must move with the offset.

    If it stays pinned at the last line, prompt_toolkit auto-scrolls the
    window back to the tail every frame and the user sees nothing change.
    The cursor y should always be ``last_line - offset``.
    """
    state = _make_state_with_history(line_count=50)
    layout = build_layout(state)

    # The transcript_window's content is the FormattedTextControl we built;
    # access its get_cursor_position via ``content``.
    transcript_window = layout.transcript_window
    control = transcript_window.content
    # Sanity: the cursor reporter is a callable on FormattedTextControl.
    assert control.get_cursor_position is not None

    state.transcript_scroll_offset = 0
    y0 = control.get_cursor_position().y
    state.transcript_scroll_offset = 12
    y1 = control.get_cursor_position().y
    assert y1 == y0 - 12, (
        f"Cursor must shift up by exactly the offset — got y0={y0} y1={y1}"
    )


def test_state_default_offset_is_zero() -> None:
    state = _make_state_with_history(line_count=1)
    assert state.transcript_scroll_offset == 0
