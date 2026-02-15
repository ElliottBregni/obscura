"""
sdk.tui.widgets.message_list -- Scrollable conversation view.

A vertically scrolling container that holds MessageBubble widgets
in chronological order. Supports auto-scrolling to the latest message
during streaming, and manual scroll override.
"""

from __future__ import annotations

from typing import cast

from textual.containers import VerticalScroll
from textual.reactive import reactive
from textual.widgets import Static

from sdk.tui.widgets.message_bubble import MessageBubble


class MessageList(VerticalScroll):
    """Scrollable conversation history.

    Holds MessageBubble widgets and auto-scrolls to the bottom
    during streaming. The user can scroll up to review history,
    which pauses auto-scroll until they scroll back to the bottom.
    """

    DEFAULT_CSS = """
    MessageList {
        height: 1fr;
        overflow-y: auto;
        padding: 0 1;
    }
    """

    auto_scroll_enabled: reactive[bool] = reactive(True)

    def __init__(
        self,
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id or "message-list", classes=classes)
        self._current_bubble: MessageBubble | None = None
        self._message_count: int = 0

    # -- Message management -------------------------------------------------

    async def add_user_message(self, content: str) -> MessageBubble:
        """Add a user message to the conversation.

        Args:
            content: The user's message text.

        Returns:
            The created MessageBubble widget.
        """
        self._message_count += 1
        bubble = MessageBubble(
            role="user",
            content=content,
            id=f"msg-{self._message_count}",
        )
        await self.mount(bubble)
        self._scroll_to_bottom()
        return bubble

    async def add_assistant_message(self, content: str = "") -> MessageBubble:
        """Add an assistant message bubble (may be empty for streaming).

        Args:
            content: Initial content (empty for streaming).

        Returns:
            The created MessageBubble widget.
        """
        self._message_count += 1
        bubble = MessageBubble(
            role="assistant",
            content=content,
            id=f"msg-{self._message_count}",
        )
        self._current_bubble = bubble
        await self.mount(bubble)
        self._scroll_to_bottom()
        return bubble

    def add_system_message(self, content: str) -> None:
        """Add a system/info message (not a chat message).

        Used for mode switches, errors, session info, etc.
        """
        self._message_count += 1
        msg = Static(
            content,
            classes="system-message",
            id=f"msg-{self._message_count}",
        )
        self.mount(msg)
        self._scroll_to_bottom()

    @property
    def current_bubble(self) -> MessageBubble | None:
        """The currently streaming assistant bubble."""
        return self._current_bubble

    def clear_current(self) -> None:
        """Clear the reference to the current streaming bubble."""
        self._current_bubble = None

    def clear_all(self) -> None:
        """Remove all messages from the list."""
        for child in list(self.children):
            child.remove()
        self._current_bubble = None
        self._message_count = 0

    # -- Scrolling ----------------------------------------------------------

    def _scroll_to_bottom(self) -> None:
        """Scroll to the bottom of the message list."""
        if self.auto_scroll_enabled:
            self.scroll_end(animate=False)

    def on_scroll_up(self) -> None:
        """User scrolled up -- pause auto-scroll."""
        self.auto_scroll_enabled = False

    def on_scroll_down(self) -> None:
        """User scrolled down -- re-enable auto-scroll if at bottom."""
        offset_y = _get_y(self.scroll_offset)
        max_y = _get_y(getattr(self, "max_scroll_offset", None))
        if offset_y >= max_y - 2:
            self.auto_scroll_enabled = True


def _get_y(offset: object) -> int:
    """Extract y attribute from Textual offset objects safely."""
    if offset is None:
        return 0
    return cast(int, getattr(offset, "y", 0))

    def request_scroll_to_bottom(self) -> None:
        """Explicitly scroll to bottom (e.g., new message posted)."""
        self.auto_scroll_enabled = True
        self.scroll_end(animate=False)
