"""
sdk.tui.widgets.tool_status -- Tool use indicator with status.

Shows tool invocations inline in the conversation with a spinner
while running and a status icon (check/cross) on completion.
"""

from __future__ import annotations

from typing import override

from textual.app import ComposeResult
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static


class ToolStatus(Widget):
    """Displays tool use status inline in the conversation.

    Shows:
    - Spinner while tool is running
    - Tool name
    - Result summary on completion
    - Error message on failure

    States: 'running', 'complete', 'error'
    """

    DEFAULT_CSS = """
    ToolStatus {
        height: auto;
        margin: 0 2;
        padding: 0 1;
    }
    """

    status: reactive[str] = reactive("running")

    def __init__(
        self,
        tool_name: str = "",
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes)
        self._tool_name = tool_name
        self._input_text = ""
        self._result_text = ""
        self._name_widget: Static | None = None
        self._result_widget: Static | None = None

    @override
    def compose(self) -> ComposeResult:
        yield Static(
            self._format_name_line(),
            classes="tool-name",
        )
        yield Static("", classes="tool-result")

    def on_mount(self) -> None:
        """Cache child widget references."""
        children = list(self.query(Static))
        if len(children) >= 2:
            self._name_widget = children[0]
            self._result_widget = children[1]
        self.add_class("tool-status")

    def _format_name_line(self) -> str:
        """Format the tool name line with status indicator."""
        if self.status == "running":
            icon = "..."
        elif self.status == "complete":
            icon = "ok"
        elif self.status == "error":
            icon = "ERR"
        else:
            icon = "?"
        return f"[{icon}] {self._tool_name}"

    # -- Public API ---------------------------------------------------------

    def start(self, tool_name: str) -> None:
        """Start showing a new tool invocation."""
        self._tool_name = tool_name
        self._input_text = ""
        self._result_text = ""
        self.status = "running"
        self.remove_class("complete")
        self.remove_class("error")
        if self._name_widget:
            self._name_widget.update(self._format_name_line())
        if self._result_widget:
            self._result_widget.update("")

    def update_input(self, delta: str) -> None:
        """Append tool input delta (for streaming tool inputs)."""
        self._input_text += delta

    def complete(self, result: str) -> None:
        """Mark the tool as completed with a result."""
        self._result_text = result
        self.status = "complete"
        self.remove_class("error")
        self.add_class("complete")
        if self._name_widget:
            self._name_widget.update(self._format_name_line())
        if self._result_widget:
            # Show a truncated result preview
            preview = result[:200] if result else "(no output)"
            if len(result) > 200:
                preview += "..."
            self._result_widget.update(preview)

    def fail(self, error: str) -> None:
        """Mark the tool as failed with an error."""
        self._result_text = error
        self.status = "error"
        self.remove_class("complete")
        self.add_class("error")
        if self._name_widget:
            self._name_widget.update(self._format_name_line())
        if self._result_widget:
            self._result_widget.update(f"Error: {error}")

    def watch_status(self, value: str) -> None:
        """React to status changes."""
        if self._name_widget:
            self._name_widget.update(self._format_name_line())
