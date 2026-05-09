"""Tests for the UiEvent contract."""

from __future__ import annotations

from obscura.cli.renderer.ui_event import (
    DisplayMode,
    UiEvent,
    UiEventKind,
    UiEventSource,
    UiSeverity,
    UiVisibility,
)


def test_ui_event_defaults() -> None:
    e = UiEvent(kind=UiEventKind.MESSAGE, source=UiEventSource.AGENT)
    assert e.kind == UiEventKind.MESSAGE
    assert e.source == UiEventSource.AGENT
    assert e.visibility == UiVisibility.NORMAL
    assert e.severity == UiSeverity.INFO
    assert e.metadata == {}
    assert e.id  # auto-generated id is non-empty
    assert e.ts is not None


def test_ids_are_unique() -> None:
    a = UiEvent(kind=UiEventKind.STATUS, source=UiEventSource.SYSTEM)
    b = UiEvent(kind=UiEventKind.STATUS, source=UiEventSource.SYSTEM)
    assert a.id != b.id


def test_is_visible_normal_vs_debug() -> None:
    visible = UiEvent(
        kind=UiEventKind.MESSAGE,
        source=UiEventSource.AGENT,
        visibility=UiVisibility.NORMAL,
    )
    hidden = UiEvent(
        kind=UiEventKind.DEBUG,
        source=UiEventSource.RUNTIME,
        visibility=UiVisibility.HIDDEN,
    )
    debug_only = UiEvent(
        kind=UiEventKind.DEBUG,
        source=UiEventSource.RUNTIME,
        visibility=UiVisibility.DEBUG_ONLY,
    )

    assert visible.is_visible(DisplayMode.NORMAL)
    assert visible.is_visible(DisplayMode.DEBUG)
    assert not hidden.is_visible(DisplayMode.NORMAL)
    assert not hidden.is_visible(DisplayMode.DEBUG)
    assert not debug_only.is_visible(DisplayMode.NORMAL)
    assert debug_only.is_visible(DisplayMode.DEBUG)


def test_collapsed_still_visible() -> None:
    e = UiEvent(
        kind=UiEventKind.TOOL_RESULT,
        source=UiEventSource.TOOL,
        visibility=UiVisibility.COLLAPSED,
    )
    assert e.is_visible(DisplayMode.NORMAL)
    assert e.is_visible(DisplayMode.DEBUG)
