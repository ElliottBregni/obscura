"""obscura.cli.renderer — Renderer factory.

Creates the frame-buffered modern renderer for the REPL event stream.

The renderer consumes :class:`AgentEvent` instances; internally each
event is projected onto :class:`UiEvent` via
:class:`SignalNormalizer` (see :mod:`normalizer`) so the rendering
body stays provider-agnostic and respects the active display mode.
"""

from __future__ import annotations

from obscura.cli.renderer.protocol import RendererProtocol
from obscura.cli.renderer.ui_event import (
    DisplayMode,
    UiEvent,
    UiEventKind,
    UiEventSource,
    UiSeverity,
    UiVisibility,
)


def create_renderer(
    streaming_status: object | None = None,
    *,
    display_mode: DisplayMode | str = DisplayMode.NORMAL,
) -> RendererProtocol:
    """Create the renderer for the REPL event stream.

    ``display_mode`` controls UiEvent visibility filtering inside the
    :class:`SignalNormalizer`. NORMAL (default) hides debug noise;
    DEBUG surfaces raw provider payloads, adapter decisions, and full
    tool args/results.
    """
    from obscura.cli.renderer.modern.renderer import ModernRenderer

    return ModernRenderer(
        streaming_status=streaming_status,
        display_mode=display_mode,
    )


__all__ = [
    "DisplayMode",
    "RendererProtocol",
    "UiEvent",
    "UiEventKind",
    "UiEventSource",
    "UiSeverity",
    "UiVisibility",
    "create_renderer",
]
