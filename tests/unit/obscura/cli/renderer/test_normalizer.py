"""Tests for SignalNormalizer behaviour.

Covers the acceptance criteria spelled out in the architecture spec:

* streaming text deltas → MESSAGE UiEvents (provider-agnostic)
* tool_call ↔ tool_result correlate via correlation_id
* long tool results collapse in normal mode
* raw payloads only appear in debug mode
* malformed AgentEvent doesn't crash — emits an error UiEvent
* duplicate status events deduped in normal mode
* normal mode hides debug noise
"""

from __future__ import annotations

import time

import pytest

from obscura.cli.renderer.normalizer import NormalizerConfig, SignalNormalizer
from obscura.cli.renderer.ui_event import (
    DisplayMode,
    UiEventKind,
    UiEventSource,
    UiVisibility,
)
from obscura.core.enums.agent import AgentEventKind
from obscura.core.types import AgentEvent


def _evt(kind: AgentEventKind, **kwargs) -> AgentEvent:
    return AgentEvent(kind=kind, **kwargs)


# ── Streaming text ───────────────────────────────────────────────────────


def test_text_delta_normalizes_to_message_event() -> None:
    norm = SignalNormalizer(mode=DisplayMode.NORMAL)
    out = norm.normalize(_evt(AgentEventKind.TEXT_DELTA, text="Hello"))
    assert len(out) == 1
    ui = out[0]
    assert ui.kind == UiEventKind.MESSAGE
    assert ui.source == UiEventSource.AGENT
    assert ui.content == "Hello"
    assert ui.visibility == UiVisibility.NORMAL


def test_streaming_chunks_pass_through_in_order() -> None:
    norm = SignalNormalizer(mode=DisplayMode.NORMAL)
    chunks = ["Hel", "lo, ", "world"]
    received: list[str] = []
    for c in chunks:
        for ui in norm.normalize(_evt(AgentEventKind.TEXT_DELTA, text=c)):
            received.append(ui.content)  # type: ignore[arg-type]
    assert "".join(received) == "Hello, world"


# ── Tool call / result correlation ───────────────────────────────────────


def test_tool_call_and_result_share_correlation_id() -> None:
    norm = SignalNormalizer(mode=DisplayMode.NORMAL)
    call = norm.normalize(
        _evt(
            AgentEventKind.TOOL_CALL,
            tool_name="read_file",
            tool_input={"path": "x.py"},
            tool_use_id="toolu_1",
        ),
    )
    result = norm.normalize(
        _evt(
            AgentEventKind.TOOL_RESULT,
            tool_name="read_file",
            tool_result="file contents",
            tool_use_id="toolu_1",
        ),
    )
    assert call[0].correlation_id == "toolu_1"
    assert result[0].correlation_id == "toolu_1"
    assert call[0].kind == UiEventKind.TOOL_CALL
    assert result[0].kind == UiEventKind.TOOL_RESULT


def test_unmatched_tool_result_tagged_in_metadata() -> None:
    norm = SignalNormalizer(mode=DisplayMode.NORMAL)
    out = norm.normalize(
        _evt(
            AgentEventKind.TOOL_RESULT,
            tool_name="read_file",
            tool_result="oops",
            tool_use_id="orphan_id",
        ),
    )
    assert out[0].metadata.get("unmatched_call") is True


# ── Collapsing ───────────────────────────────────────────────────────────


def test_long_tool_result_collapses_in_normal_mode() -> None:
    norm = SignalNormalizer(
        mode=DisplayMode.NORMAL,
        config=NormalizerConfig(collapse_lines=10, collapse_bytes=10_000),
    )
    long_result = "\n".join(f"line {i}" for i in range(50))
    out = norm.normalize(
        _evt(
            AgentEventKind.TOOL_RESULT,
            tool_name="run_command",
            tool_result=long_result,
            tool_use_id="tu_2",
        ),
    )
    assert out[0].visibility == UiVisibility.COLLAPSED
    assert out[0].metadata.get("collapsed") is True


def test_long_tool_result_does_not_collapse_in_debug_mode() -> None:
    norm = SignalNormalizer(
        mode=DisplayMode.DEBUG,
        config=NormalizerConfig(collapse_lines=10, collapse_bytes=10_000),
    )
    long_result = "\n".join(f"line {i}" for i in range(50))
    out = norm.normalize(
        _evt(
            AgentEventKind.TOOL_RESULT,
            tool_name="run_command",
            tool_result=long_result,
            tool_use_id="tu_3",
        ),
    )
    # First UiEvent is the user-facing TOOL_RESULT; it should not be
    # collapsed in debug mode. A debug mirror may follow.
    primary = next(ui for ui in out if ui.kind == UiEventKind.TOOL_RESULT)
    assert primary.visibility == UiVisibility.NORMAL


# ── Raw payload exposure ─────────────────────────────────────────────────


def test_raw_stripped_in_normal_mode() -> None:
    norm = SignalNormalizer(mode=DisplayMode.NORMAL)
    out = norm.normalize(
        _evt(
            AgentEventKind.TOOL_CALL,
            tool_name="read_file",
            tool_input={"path": "x.py"},
            tool_use_id="tu_4",
        ),
    )
    assert out[0].raw is None


def test_raw_preserved_in_debug_mode() -> None:
    norm = SignalNormalizer(mode=DisplayMode.DEBUG)
    out = norm.normalize(
        _evt(
            AgentEventKind.TOOL_CALL,
            tool_name="read_file",
            tool_input={"path": "x.py"},
            tool_use_id="tu_5",
        ),
    )
    primary = out[0]
    assert primary.raw is not None
    # Debug mirror also emitted with raw payload.
    debug_mirrors = [ui for ui in out if ui.kind == UiEventKind.DEBUG]
    assert len(debug_mirrors) == 1


# ── Malformed events ─────────────────────────────────────────────────────


def test_malformed_event_does_not_crash() -> None:
    norm = SignalNormalizer(mode=DisplayMode.NORMAL)

    class Junk:
        # No `kind` attr — adapter chain falls into the catch-all
        # AttributeError path in RuntimeEventAdapter.adapt().
        pass

    out = norm.normalize(Junk())  # type: ignore[arg-type]
    assert any(ui.kind == UiEventKind.ERROR for ui in out)


# ── Status dedup ─────────────────────────────────────────────────────────


def test_duplicate_status_events_deduped_in_normal_mode() -> None:
    norm = SignalNormalizer(
        mode=DisplayMode.NORMAL,
        config=NormalizerConfig(status_dedup_window_s=10.0),
    )
    e = _evt(AgentEventKind.RATE_LIMIT_WARNING, text="approaching limit")
    first = norm.normalize(e)
    second = norm.normalize(e)
    assert any(ui.kind == UiEventKind.STATUS for ui in first)
    # Inside the dedup window the second emission is dropped.
    assert all(ui.kind != UiEventKind.STATUS for ui in second)


def test_status_dedup_window_expires() -> None:
    norm = SignalNormalizer(
        mode=DisplayMode.NORMAL,
        config=NormalizerConfig(status_dedup_window_s=0.001),
    )
    e = _evt(AgentEventKind.RATE_LIMIT_WARNING, text="approaching limit")
    first = norm.normalize(e)
    time.sleep(0.01)
    second = norm.normalize(e)
    assert any(ui.kind == UiEventKind.STATUS for ui in first)
    assert any(ui.kind == UiEventKind.STATUS for ui in second)


def test_status_dedup_disabled_in_debug_mode() -> None:
    norm = SignalNormalizer(
        mode=DisplayMode.DEBUG,
        config=NormalizerConfig(status_dedup_window_s=10.0),
    )
    e = _evt(AgentEventKind.RATE_LIMIT_WARNING, text="approaching limit")
    first = norm.normalize(e)
    second = norm.normalize(e)
    assert any(ui.kind == UiEventKind.STATUS for ui in first)
    # Debug mode keeps every status event so the trace is faithful.
    assert any(ui.kind == UiEventKind.STATUS for ui in second)


# ── Visibility filtering ─────────────────────────────────────────────────


def test_normal_mode_hides_debug_only_events() -> None:
    norm = SignalNormalizer(mode=DisplayMode.NORMAL)
    out = norm.normalize(_evt(AgentEventKind.AGENT_START))
    # AGENT_START maps to a DEBUG_ONLY UiEvent — dropped in normal mode.
    assert out == []


def test_debug_mode_surfaces_debug_only_events() -> None:
    norm = SignalNormalizer(mode=DisplayMode.DEBUG)
    out = norm.normalize(_evt(AgentEventKind.AGENT_START))
    assert any(ui.kind == UiEventKind.DEBUG for ui in out)


def test_runtime_layout_events_pass_through() -> None:
    """TURN_START / AGENT_DONE flow through as TRACE so the renderer
    can drive its frame buffer; visibility is NORMAL so the renderer
    sees them in both modes."""
    norm = SignalNormalizer(mode=DisplayMode.NORMAL)
    out = norm.normalize(_evt(AgentEventKind.TURN_START))
    assert any(ui.kind == UiEventKind.TRACE for ui in out)


def test_set_mode_clears_dedup_state() -> None:
    norm = SignalNormalizer(
        mode=DisplayMode.NORMAL,
        config=NormalizerConfig(status_dedup_window_s=10.0),
    )
    e = _evt(AgentEventKind.RATE_LIMIT_WARNING, text="approaching limit")
    norm.normalize(e)
    norm.set_mode(DisplayMode.DEBUG)
    norm.set_mode(DisplayMode.NORMAL)
    out = norm.normalize(e)
    assert any(ui.kind == UiEventKind.STATUS for ui in out)


# ── Adapter resilience ───────────────────────────────────────────────────


def test_adapter_exception_surfaces_as_error_event() -> None:
    """If a custom adapter throws, the normalizer yields an error
    UiEvent rather than crashing the TUI."""
    from obscura.cli.renderer.adapters.base import EventAdapter

    class ExplodingAdapter(EventAdapter):
        def handles(self, event):  # noqa: ARG002
            return True

        def adapt(self, event):  # noqa: ARG002
            raise RuntimeError("kaboom")

    norm = SignalNormalizer(
        mode=DisplayMode.NORMAL,
        adapters=[ExplodingAdapter()],
    )
    out = norm.normalize(_evt(AgentEventKind.TEXT_DELTA, text="x"))
    assert len(out) == 1
    assert out[0].kind == UiEventKind.ERROR


# ── Error events stay visible regardless of mode ────────────────────────


@pytest.mark.parametrize("mode", [DisplayMode.NORMAL, DisplayMode.DEBUG])
def test_error_events_visible_in_all_modes(mode: DisplayMode) -> None:
    norm = SignalNormalizer(mode=mode)
    out = norm.normalize(_evt(AgentEventKind.ERROR, text="boom"))
    assert any(ui.kind == UiEventKind.ERROR for ui in out)
