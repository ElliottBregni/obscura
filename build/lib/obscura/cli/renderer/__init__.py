"""obscura.cli.renderer — Renderer factory.

Creates the frame-buffered modern renderer for the REPL event stream.
"""

from __future__ import annotations

from obscura.cli.renderer.protocol import RendererProtocol


def create_renderer(
    streaming_status: object | None = None,
) -> RendererProtocol:
    """Create the renderer for the REPL event stream."""
    from obscura.cli.renderer.modern.renderer import ModernRenderer

    return ModernRenderer(streaming_status=streaming_status)


__all__ = ["RendererProtocol", "create_renderer"]
