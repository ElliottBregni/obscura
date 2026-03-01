"""
obscura.tui.widgets.thinking_block -- Collapsible thinking/reasoning content.

Displays the model's internal reasoning in a collapsible block.
Collapsed by default, toggled with the 't' key.
"""

from __future__ import annotations

from typing import override

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static


class ThinkingBlock(Widget):
    """A collapsible block showing the model's thinking/reasoning.

    Attributes:
        collapsed: Whether the thinking content is hidden.
    """

    DEFAULT_CSS = """
    ThinkingBlock {
        height: auto;
        margin: 0;
        padding: 0;
    }
    """

    collapsed: reactive[bool] = reactive(True)

    def __init__(
        self,
        content: str = "",
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes)
        self._content = content
        self._header_widget: Static | None = None
        self._content_widget: Static | None = None

    @override
    def compose(self) -> ComposeResult:
        with Vertical(classes="thinking-block collapsed"):
            yield Static(
                self._header_text,
                classes="thinking-header",
            )
            yield Static(
                self._content or "(thinking...)",
                classes="thinking-content",
            )

    def on_mount(self) -> None:
        """Cache references to child widgets."""
        children = list(self.query(Static))
        if len(children) >= 2:
            self._header_widget = children[0]
            self._content_widget = children[1]
        self._update_collapsed_display()

    @property
    def _header_text(self) -> str:
        """Build the header text with collapse indicator."""
        arrow = ">" if self.collapsed else "v"
        preview = ""
        if self.collapsed and self._content:
            # Show first 40 chars as preview
            preview = f" {self._content[:40]}..."
        return f"[{arrow}] Thinking{preview}"

    def append(self, text: str) -> None:
        """Append text to the thinking content (used during streaming)."""
        self._content += text
        if self._content_widget:
            self._content_widget.update(self._content)
        if self._header_widget:
            self._header_widget.update(self._header_text)

    def set_content(self, text: str) -> None:
        """Replace the full thinking content."""
        self._content = text
        if self._content_widget:
            self._content_widget.update(self._content)
        if self._header_widget:
            self._header_widget.update(self._header_text)

    def toggle(self) -> None:
        """Toggle collapsed/expanded state."""
        self.collapsed = not self.collapsed

    def watch_collapsed(self, value: bool) -> None:
        """React to collapsed state changes."""
        self._update_collapsed_display()

    def _update_collapsed_display(self) -> None:
        """Show/hide the content based on collapsed state."""
        try:
            container = self.query_one(Vertical)
        except Exception:
            return

        if self.collapsed:
            container.add_class("collapsed")
        else:
            container.remove_class("collapsed")

        if self._header_widget:
            self._header_widget.update(self._header_text)
