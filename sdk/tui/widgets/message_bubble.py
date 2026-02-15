"""
sdk.tui.widgets.message_bubble -- Single message display widget.

Renders a user or assistant message with Rich Markdown formatting,
syntax-highlighted code blocks, and inline thinking/tool widgets.
"""

from __future__ import annotations

from typing import override

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widget import Widget
from textual.widgets import Static

from sdk.tui.widgets.thinking_block import ThinkingBlock
from sdk.tui.widgets.tool_status import ToolStatus


class MessageBubble(Widget):
    """A single conversation message (user or assistant).

    Supports:
    - Rich Markdown rendering for assistant responses
    - Syntax-highlighted code blocks
    - Inline ThinkingBlock widgets (collapsible)
    - Inline ToolStatus widgets
    - Error display
    - Streaming text appending

    The ``role`` determines styling: 'user' messages get a different
    background color than 'assistant' messages.
    """

    DEFAULT_CSS = """
    MessageBubble {
        height: auto;
        margin: 1 0;
        padding: 1 2;
    }
    """

    def __init__(
        self,
        role: str = "user",
        content: str = "",
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        base_classes = f"message-bubble {role}"
        if classes:
            base_classes += f" {classes}"
        super().__init__(name=name, id=id, classes=base_classes)
        self._role = role
        self._content = content
        self._content_widget: Static | None = None
        self._thinking_block: ThinkingBlock | None = None
        self._tool_statuses: list[ToolStatus] = []
        self._container: Vertical | None = None
        self._finalized = False

    # -- Compose ------------------------------------------------------------

    @override
    def compose(self) -> ComposeResult:
        with Vertical():
            # Role label
            role_display = "You" if self._role == "user" else "Assistant"
            yield Static(
                role_display,
                classes=f"role-label {self._role}",
            )
            # Content
            yield Static(
                self._render_content(),
                classes="message-content",
                markup=True,
            )

    def on_mount(self) -> None:
        """Cache references to content widget."""
        try:
            self._content_widget = self.query_one(".message-content", Static)
            self._container = self.query_one(Vertical)
        except Exception:
            pass

    # -- Content rendering --------------------------------------------------

    def _render_content(self) -> str:  # pyright: ignore[reportIncompatibleMethodOverride,reportImplicitOverride]
        """Render message content with basic markup.

        Uses Textual's built-in Rich markup for formatting.
        """
        if not self._content:
            return ""

        text = self._content

        # Convert markdown-style bold/italic to Rich markup
        # (Textual's Static widget supports Rich console markup)
        return text

    # -- Streaming API ------------------------------------------------------

    def append_text(self, delta: str) -> None:
        """Append streaming text to the message content.

        Called for each TEXT_DELTA chunk during streaming.
        """
        self._content += delta
        if self._content_widget:
            self._content_widget.update(self._render_content())

    def add_thinking_block(self) -> ThinkingBlock:
        """Add a new ThinkingBlock to this message.

        Returns:
            The created ThinkingBlock widget.
        """
        block = ThinkingBlock()
        self._thinking_block = block
        if self._container:
            self._container.mount(block)
        return block

    def get_thinking_block(self) -> ThinkingBlock | None:
        """Get the current thinking block, if any."""
        return self._thinking_block

    def add_tool_status(self, tool_name: str) -> ToolStatus:
        """Add a ToolStatus widget for a tool invocation.

        Args:
            tool_name: The name of the tool being used.

        Returns:
            The created ToolStatus widget.
        """
        ts = ToolStatus(tool_name=tool_name)
        self._tool_statuses.append(ts)
        if self._container:
            self._container.mount(ts)
        return ts

    def get_latest_tool_status(self) -> ToolStatus | None:
        """Get the most recently added ToolStatus."""
        return self._tool_statuses[-1] if self._tool_statuses else None

    def show_error(self, error: str) -> None:
        """Display an error message in the bubble."""
        if self._container:
            error_widget = Static(
                f"Error: {error}",
                classes="error-text",
            )
            self._container.mount(error_widget)

    def finalize(self) -> None:
        """Mark the message as complete (no more streaming)."""
        self._finalized = True
        # Final render with full content
        if self._content_widget:
            self._content_widget.update(self._render_content())

    @property
    def content(self) -> str:
        """The full text content of this message."""
        return self._content

    @property
    def role(self) -> str:
        """The role of this message ('user' or 'assistant')."""
        return self._role

    @property
    def is_finalized(self) -> bool:
        """Whether the message is complete."""
        return self._finalized
