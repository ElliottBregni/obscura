"""obscura.cli.renderer.modern.compositor — Component tree → FrameBuffer.

Walks the component tree with layout results and calls each component's
``render()`` method to write styled cells into the frame buffer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from obscura.cli.renderer.modern.components import Component, RootComponent
from obscura.cli.renderer.modern.layout import LayoutEngine, LayoutResult, Region

if TYPE_CHECKING:
    from obscura.cli.renderer.modern.frame_buffer import FrameBuffer


class Compositor:
    """Composites a component tree into a :class:`FrameBuffer`.

    Workflow per frame:
    1. ``LayoutEngine.layout()`` computes regions for each component.
    2. ``Compositor.composite()`` walks the tree and calls ``render()``.
    """

    def __init__(self) -> None:
        self._layout_engine = LayoutEngine()

    def composite(
        self,
        root: RootComponent,
        buf: FrameBuffer,
        terminal_width: int,
        terminal_height: int,
    ) -> int:
        """Render the full component tree into the buffer.

        Returns the total number of rows consumed.
        """
        result = self._layout_engine.layout(root, terminal_width, terminal_height)
        return self._render_tree(
            root,
            buf,
            result,
            Region(
                x=0,
                y=0,
                width=terminal_width,
                height=terminal_height,
            ),
        )

    def _render_tree(
        self,
        node: Component,
        buf: FrameBuffer,
        layout: LayoutResult,
        fallback_region: Region,
    ) -> int:
        """Recursively render a component and its children."""
        if not node.visible:
            return 0

        region = layout.get(id(node))
        if region.width == 0 and region.height == 0:
            region = fallback_region

        # Render the node itself
        lines = node.render(buf, region)

        # Render children (already positioned by layout)
        content_region = region.inner(node.style)
        cursor_y = content_region.y
        for child in node.children:
            if not child.visible:
                continue
            child_region = layout.get(id(child))
            if child_region.width == 0 and child_region.height == 0:
                _w, h = child.measure(content_region.width)
                child_region = Region(
                    x=content_region.x,
                    y=cursor_y,
                    width=content_region.width,
                    height=h,
                )
            child_lines = self._render_tree(child, buf, layout, child_region)
            cursor_y += child_lines

        return lines
