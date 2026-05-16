"""Tests for the pure ``FormattedText`` factories in ``obscura.cli.tui.buffers``."""

from __future__ import annotations

import pytest

from obscura.cli.tui.buffers import (
    header_text,
    live_region_text,
    transcript_text,
)
from obscura.cli.tui.state import (
    HUDState,
    LiveRegionKind,
    LiveRegionState,
    StyledRun,
    TranscriptEntry,
    TranscriptKind,
    TUIState,
)

pytestmark = pytest.mark.unit


def _make_state() -> TUIState:
    hud = HUDState(
        backend="copilot",
        model="gpt-4o",
        session_id="abcdef0123456789",
    )
    return TUIState(hud=hud)


def test_transcript_text_empty_state_renders_welcome_hint() -> None:
    state = _make_state()
    out = transcript_text(state)
    # Empty transcript shows a welcome hint so the launch screen isn't a
    # void; non-empty text and no exception.
    assert list(out)
    flat = "".join(text for _, text in out)
    assert "Welcome to Obscura" in flat
    assert "Enter sends" in flat
    assert "Ctrl+K command palette" in flat


def test_transcript_text_with_entries_returns_styled_tuples() -> None:
    state = _make_state()
    state.append_transcript(
        TranscriptEntry(
            kind=TranscriptKind.USER,
            runs=[StyledRun(text="hi")],
        )
    )
    state.append_transcript(
        TranscriptEntry(
            kind=TranscriptKind.ASSISTANT,
            runs=[StyledRun(text="hello back")],
        )
    )
    state.append_transcript(
        TranscriptEntry(
            kind=TranscriptKind.TOOL_USE,
            runs=[StyledRun(text="bash command=ls")],
        )
    )

    out = list(transcript_text(state))
    assert out, "expected non-empty list of (style, text) tuples"
    for item in out:
        assert isinstance(item, tuple)
        assert len(item) == 2
        style, text = item
        assert isinstance(style, str)
        assert isinstance(text, str)
    # Bodies of all three entries appear somewhere in the rendered tuples.
    flat = "".join(text for _, text in out)
    assert "hi" in flat
    assert "hello back" in flat
    assert "bash command=ls" in flat


def test_live_region_text_idle_returns_empty() -> None:
    state = _make_state()
    state.live = LiveRegionState(kind=LiveRegionKind.IDLE)
    out = live_region_text(state)
    assert list(out) == []


def test_live_region_text_active_contains_label() -> None:
    state = _make_state()
    state.live = LiveRegionState(
        kind=LiveRegionKind.STREAMING,
        label="streaming",
    )
    out = list(live_region_text(state))
    flat = "".join(text for _, text in out)
    assert "streaming" in flat


def test_header_text_contains_model_name_and_short_session_id() -> None:
    state = _make_state()
    out = list(header_text(state))
    flat = "".join(text for _, text in out)
    # Model name is rendered literally.
    assert "gpt-4o" in flat
    # Short session id (first 8 chars) appears when there's no title.
    assert state.hud.session_id[:8] in flat
    # Backend label appears too.
    assert "copilot" in flat
    # Mode + permission mode are shown in the footer header.
    assert "chat" in flat
    assert "confirm" in flat
