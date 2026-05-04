"""obscura.cli.renderer.modern.layout — Box model and layout engine.

Provides a simple vertical box model with padding, margin, and border
support.  Components are stacked top-to-bottom; no horizontal flex is
needed for a CLI streaming renderer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import cast

from obscura.core.enums.ui import BorderStyle


# Border character sets indexed by BorderStyle
_BORDER_CHARS: dict[BorderStyle, tuple[str, str, str, str, str, str]] = {
    BorderStyle.LIGHT: ("─", "│", "┌", "┐", "└", "┘"),
    BorderStyle.HEAVY: ("━", "┃", "┏", "┓", "┗", "┛"),
    BorderStyle.ROUND: ("─", "│", "╭", "╮", "╰", "╯"),
    BorderStyle.DOUBLE: ("═", "║", "╔", "╗", "╚", "╝"),
}


def get_border_chars(style: BorderStyle) -> tuple[str, str, str, str, str, str]:
    """Return (horiz, vert, tl, tr, bl, br) for the given border style."""
    return _BORDER_CHARS.get(style, ("─", "│", "┌", "┐", "└", "┘"))


@dataclass(frozen=True)
class BoxStyle:
    """Immutable box model for component layout."""

    padding_top: int = 0
    padding_bottom: int = 0
    padding_left: int = 0
    padding_right: int = 0
    margin_top: int = 0
    margin_bottom: int = 0
    margin_left: int = 0
    margin_right: int = 0
    border: BorderStyle = BorderStyle.NONE
    max_width: int | None = None

    @property
    def horizontal_padding(self) -> int:
        return self.padding_left + self.padding_right

    @property
    def vertical_padding(self) -> int:
        return self.padding_top + self.padding_bottom

    @property
    def horizontal_margin(self) -> int:
        return self.margin_left + self.margin_right

    @property
    def vertical_margin(self) -> int:
        return self.margin_top + self.margin_bottom

    @property
    def border_width(self) -> int:
        """Total horizontal space consumed by borders (0 or 2)."""
        return 2 if self.border != BorderStyle.NONE else 0

    @property
    def border_height(self) -> int:
        """Total vertical space consumed by borders (0 or 2)."""
        return 2 if self.border != BorderStyle.NONE else 0

    @property
    def horizontal_overhead(self) -> int:
        """Total horizontal space consumed by margin + border + padding."""
        return self.horizontal_margin + self.border_width + self.horizontal_padding

    @property
    def vertical_overhead(self) -> int:
        """Total vertical space consumed by margin + border + padding."""
        return self.vertical_margin + self.border_height + self.vertical_padding


@dataclass
class Region:
    """A rectangular area within the terminal."""

    x: int = 0
    y: int = 0
    width: int = 0
    height: int = 0

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def bottom(self) -> int:
        return self.y + self.height

    def inner(self, style: BoxStyle) -> Region:
        """Return the content region after subtracting margin/border/padding."""
        bw = 1 if style.border != BorderStyle.NONE else 0
        return Region(
            x=self.x + style.margin_left + bw + style.padding_left,
            y=self.y + style.margin_top + bw + style.padding_top,
            width=max(0, self.width - style.horizontal_overhead),
            height=max(0, self.height - style.vertical_overhead),
        )


@dataclass
class LayoutResult:
    """Mapping from components to their allocated regions."""

    regions: dict[int, Region] = field(
        default_factory=lambda: cast(dict[int, Region], {})
    )

    def get(self, component_id: int) -> Region:
        return self.regions.get(component_id, Region())

    def set(self, component_id: int, region: Region) -> None:
        self.regions[component_id] = region


class LayoutEngine:
    """Simple top-down vertical layout allocator.

    Walks a component tree and assigns :class:`Region` to each component
    by stacking children vertically within the parent's content area.
    """

    def layout(
        self,
        root: object,
        terminal_width: int,
        terminal_height: int,
    ) -> LayoutResult:
        """Compute layout for the entire component tree.

        Parameters
        ----------
        root:
            Root component (duck-typed: needs ``style``, ``children``,
            ``measure(width)`` attributes).
        terminal_width:
            Available terminal columns.
        terminal_height:
            Available terminal rows.

        """
        result = LayoutResult()
        root_region = Region(x=0, y=0, width=terminal_width, height=terminal_height)
        self._layout_node(root, root_region, result)
        return result

    def _layout_node(
        self,
        node: object,
        available: Region,
        result: LayoutResult,
    ) -> int:
        """Recursively layout a node and its children.

        Returns the total height consumed by this node.
        """
        from obscura.cli.renderer.modern.components import Component

        if not isinstance(node, Component):
            return 0

        if not node.visible:
            return 0

        style = node.style

        # Clamp width to max_width if specified
        effective_width = available.width
        if style.max_width is not None:
            effective_width = min(effective_width, style.max_width)

        # Measure this node's natural height
        _min_w, natural_height = node.measure(
            max(0, effective_width - style.horizontal_overhead),
        )

        # Allocate outer region
        outer = Region(
            x=available.x,
            y=available.y,
            width=effective_width,
            height=natural_height + style.vertical_overhead,
        )
        result.set(id(node), outer)

        # Layout children within the content area
        content = outer.inner(style)
        cursor_y = content.y
        for child in node.children:
            if not child.visible:
                continue
            child_height = self._layout_node(
                child,
                Region(
                    x=content.x,
                    y=cursor_y,
                    width=content.width,
                    height=max(0, content.height - (cursor_y - content.y)),
                ),
                result,
            )
            cursor_y += child_height

        return outer.height
