"""Snapshot tests for the TUI layout's actual rendered geometry.

These tests render the layout to a fixed-size :class:`Screen` and assert
on the resulting character grid. Cheaper than launching the full
``Application`` event loop, but catches the kind of layout bugs that
unit tests miss — input box ballooning to fill slack space, side panels
reserving columns when they have nothing to show, header running into
the transcript with no breathing room.

The reason this file exists: shipped a layout once where the empty
input area was 5 rows tall and the agent panel reserved 26 columns to
say "no agents running". A snapshot test would have caught both.
"""

from __future__ import annotations

import asyncio
from typing import Iterator

import pytest
from prompt_toolkit.application import Application
from prompt_toolkit.application.current import set_app
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.layout.mouse_handlers import MouseHandlers
from prompt_toolkit.layout.screen import Screen, WritePosition
from prompt_toolkit.output import DummyOutput

from obscura.cli.tui.layout import build_layout
from obscura.cli.tui.state import (
    HUDState,
    RunningAgentSnapshot,
    StyledRun,
    TranscriptEntry,
    TranscriptKind,
    TUIState,
)

pytestmark = pytest.mark.unit


def _make_state(transcript_lines: int = 5) -> TUIState:
    state = TUIState(
        hud=HUDState(backend="copilot", model="gpt-4", session_id="abcd1234efgh")
    )
    for i in range(transcript_lines):
        state.append_transcript(
            TranscriptEntry(
                kind=TranscriptKind.ASSISTANT,
                runs=[StyledRun(text=f"line {i}\n", style="")],
            )
        )
    return state


async def _render(
    state: TUIState,
    *,
    w: int = 100,
    h: int = 20,
    input_text: str = "",
) -> list[str]:
    """Render the layout to a w×h grid and return one string per row.

    ``input_text`` is set on the layout's input buffer before rendering so
    callers can probe how the layout responds to multi-line drafts.
    """
    layout = build_layout(state)
    if input_text:
        layout.input_buffer.text = input_text
    with create_pipe_input() as pipe_input:
        app = Application(
            layout=layout.layout,
            full_screen=False,
            input=pipe_input,
            output=DummyOutput(),
        )
        with set_app(app):
            screen = Screen(default_char=None)
            mh = MouseHandlers()
            wp = WritePosition(0, 0, w, h)
            layout.layout.container.write_to_screen(
                screen, mh, wp, "", erase_bg=True, z_index=0
            )
            rows: list[str] = []
            for y in range(h):
                buf = screen.data_buffer[y]
                line = "".join(
                    (buf.get(x).char if buf.get(x) is not None else " ")
                    for x in range(w)
                )
                rows.append(line.rstrip())
            return rows


@pytest.fixture
def event_loop() -> Iterator[asyncio.AbstractEventLoop]:
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


# ---------------------------------------------------------------------------
# Geometry tests
# ---------------------------------------------------------------------------


async def test_empty_input_is_exactly_one_row() -> None:
    """Regression: input ballooning to 5 rows when the buffer is empty.

    The previous bug was a static ``Dimension(min=1, max=6, preferred=1)``;
    HSplit handed the slack back to the input. A dynamic height tied to
    buffer line count fixes it. This test catches a regression by
    asserting the input row's neighbours.
    """
    state = _make_state()
    rows = await _render(state)
    # Find the row containing the prompt glyph "❯".
    input_rows = [i for i, line in enumerate(rows) if line.startswith("❯")]
    assert len(input_rows) == 1, (
        f"Expected exactly one input row, found {len(input_rows)}: {input_rows}"
    )
    input_idx = input_rows[0]
    # The row immediately before should NOT be empty (it should contain
    # the live region or transcript content), and the row immediately
    # after should be the toolbar (or live region, NOT empty).
    assert rows[input_idx + 1].strip(), (
        f"Row after input should be the toolbar, was blank. "
        f"Input row {input_idx}: {rows[input_idx]!r}, "
        f"next: {rows[input_idx + 1]!r}"
    )


async def test_agent_panel_hidden_when_no_agents() -> None:
    """Regression: panel always reserved 26 right-hand columns showing
    "Agents (0) / no agents running" — pure waste."""
    state = _make_state()
    rows = await _render(state)
    transcript_rows = [r for r in rows if "line 0" in r or "line 1" in r]
    assert transcript_rows, "Expected at least one transcript line in render"
    for row in transcript_rows:
        assert "Agents" not in row, (
            f"Agent panel should be hidden when there are 0 agents, but "
            f"transcript row contains it: {row!r}"
        )
        assert "no agents running" not in row


async def test_agent_panel_appears_when_agents_running() -> None:
    state = _make_state()
    state.hud.running_agents = [
        RunningAgentSnapshot(name="reviewer", status="running", elapsed_s=12),
    ]
    rows = await _render(state)
    has_panel = any("reviewer" in row or "Agents" in row for row in rows)
    assert has_panel, (
        f"Expected the agent panel to appear when agents are running. "
        f"Got rows: {rows[:5]!r}..."
    )


async def test_header_has_separator_below_it() -> None:
    """Regression: header crashed into the transcript with no rule line."""
    state = _make_state()
    rows = await _render(state)
    # Header is row 0; row 1 should be the separator (a tiled "─").
    assert rows[0].startswith("session "), (
        f"Row 0 should be header, was: {rows[0]!r}"
    )
    assert "─" in rows[1] and rows[1].count("─") >= 50, (
        f"Row 1 should be a tiled rule separator, was: {rows[1]!r}"
    )


async def test_toolbar_is_last_row() -> None:
    state = _make_state()
    rows = await _render(state, h=20)
    # The toolbar is identified by its hotkey hint string.
    assert "quit" in rows[-1] and "palette" in rows[-1], (
        f"Last row should be the toolbar, was: {rows[-1]!r}"
    )


async def test_transcript_content_appears_below_separator() -> None:
    state = _make_state(transcript_lines=3)
    rows = await _render(state, h=15)
    # Find the row containing "line 0" — must come after the separator
    # row, never before, and never on row 0 (header).
    content_rows = [i for i, r in enumerate(rows) if "line 0" in r]
    assert content_rows, "Expected to find 'line 0' in render"
    assert content_rows[0] >= 2, (
        f"Transcript content should appear at row 2 or later (header + "
        f"separator come first), was at row {content_rows[0]}"
    )


async def test_input_grows_when_buffer_has_newlines() -> None:
    """The dynamic height callable should let the input expand for
    multi-line drafts, capped at 6 rows."""
    state = _make_state()
    layout = build_layout(state)
    layout.input_buffer.text = "line one\nline two\nline three"
    rows = await _render(state)
    input_rows = [i for i, line in enumerate(rows) if line.startswith("❯")]
    # The prompt glyph still appears on one row, but the *visible* span
    # of the input should now be 3 — meaning the toolbar moved 2 rows
    # further down.
    toolbar_idx = next(
        i for i, r in enumerate(rows) if "quit" in r and "palette" in r
    )
    input_idx = input_rows[0]
    assert toolbar_idx - input_idx >= 3, (
        f"3-line buffer should produce >= 3 rows of input space; "
        f"input at {input_idx}, toolbar at {toolbar_idx}"
    )
