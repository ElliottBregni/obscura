"""obscura.cli.renderer — Renderer abstraction and factory.

Toggle between renderers via the ``OBSCURA_RENDERER`` environment variable:

    OBSCURA_RENDERER=modern    (default) Frame-buffered rendering with per-tool components
    OBSCURA_RENDERER=classic   Rich-based line-by-line rendering
"""

from __future__ import annotations

import os

from obscura.cli.renderer.protocol import RendererProtocol


def create_renderer(
    streaming_status: object | None = None,
    *,
    renderer_type: str | None = None,
) -> RendererProtocol:
    """Factory: select renderer based on ``OBSCURA_RENDERER`` env var.

    Parameters
    ----------
    streaming_status:
        Mutable status bag for the prompt_toolkit toolbar spinner.
    renderer_type:
        Explicit override (bypasses env var).  Useful for tests.

    """
    choice = (renderer_type or os.environ.get("OBSCURA_RENDERER", "modern")).lower()

    if choice == "classic":
        from obscura.cli.renderer.classic import ClassicRenderer

        return ClassicRenderer(streaming_status=streaming_status)

    from obscura.cli.renderer.modern.renderer import ModernRenderer

    return ModernRenderer(streaming_status=streaming_status)


__all__ = ["RendererProtocol", "create_renderer"]
