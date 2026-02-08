"""
sdk.tui.widgets.input_area -- Multi-line prompt input with keybindings.

Supports:
- Enter to submit, Shift+Enter for newline
- Mode indicator prefix: [ASK]>, [PLAN]>, etc.
- Slash command parsing
- Command history (up/down arrows)
- File path autocomplete in Code mode (Tab)
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static, TextArea

from sdk.tui.modes import TUIMode


# ---------------------------------------------------------------------------
# Custom TextArea that delegates Enter to parent
# ---------------------------------------------------------------------------

class _PromptTextArea(TextArea):
    """TextArea subclass that emits Enter as a message instead of inserting a newline."""

    class EnterPressed(Message):
        """Emitted when Enter is pressed (without Shift)."""

    async def _on_key(self, event: events.Key) -> None:
        if event.key == "enter":
            event.stop()
            event.prevent_default()
            self.post_message(self.EnterPressed())
            return
        # shift+enter inserts newline via default TextArea behavior
        await super()._on_key(event)


# ---------------------------------------------------------------------------
# Slash command definitions
# ---------------------------------------------------------------------------

@dataclass
class SlashCommand:
    """A parsed slash command."""

    command: str       # e.g., "mode", "backend", "session"
    args: list[str]    # e.g., ["ask"], ["claude"], ["load", "abc123"]
    raw: str           # The original input string


def parse_slash_command(text: str) -> SlashCommand | None:
    """Parse a slash command from input text.

    Returns None if the text is not a slash command.
    """
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None

    parts = stripped[1:].split()
    if not parts:
        return None

    return SlashCommand(
        command=parts[0].lower(),
        args=parts[1:],
        raw=stripped,
    )


# ---------------------------------------------------------------------------
# PromptInput widget
# ---------------------------------------------------------------------------

class PromptInput(Widget):
    """Multi-line prompt input area with mode indicator and slash commands.

    Emits a ``Submitted`` message when the user presses Enter.
    Shift+Enter inserts a newline.

    Slash commands (starting with /) are parsed and emitted as
    ``SlashCommandReceived`` messages instead of ``Submitted``.
    """

    DEFAULT_CSS = """
    PromptInput {
        dock: bottom;
        height: auto;
        max-height: 10;
        min-height: 3;
        padding: 0 1;
    }
    """

    mode: reactive[TUIMode] = reactive(TUIMode.ASK)

    # -- Messages -----------------------------------------------------------

    class Submitted(Message):
        """Emitted when the user submits a prompt (Enter key)."""

        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    class SlashCommandReceived(Message):
        """Emitted when the user enters a slash command."""

        def __init__(self, command: SlashCommand) -> None:
            super().__init__()
            self.command = command

    # -- Init ---------------------------------------------------------------

    def __init__(
        self,
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
        cwd: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id or "input-area", classes=classes)
        self._history: list[str] = []
        self._history_idx: int = -1
        self._cwd: str = cwd or "."
        self._text_area: TextArea | None = None
        self._prefix_widget: Static | None = None

    # -- Compose ------------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Horizontal():
            yield Static(
                self._prefix_text,
                classes="mode-prefix",
            )
            yield _PromptTextArea(
                "",
                id="prompt-textarea",
                show_line_numbers=False,
                placeholder="Enter to send, Shift+Enter for newline, /help for commands",
            )

    def on_mount(self) -> None:
        """Cache widget references and set initial focus."""
        try:
            self._text_area = self.query_one("#prompt-textarea", _PromptTextArea)
            self._prefix_widget = self.query_one(".mode-prefix", Static)
        except Exception:
            pass

    # -- Mode prefix --------------------------------------------------------

    @property
    def _prefix_text(self) -> str:
        """Build the mode indicator prefix."""
        return f"[{self.mode.value.upper()}]> "

    def watch_mode(self, value: TUIMode) -> None:
        """Update the prefix when mode changes."""
        if self._prefix_widget:
            self._prefix_widget.update(self._prefix_text)

    # -- Key handling -------------------------------------------------------

    async def on__prompt_text_area_enter_pressed(self) -> None:
        """Handle Enter key from the custom TextArea."""
        await self._submit()

    async def _submit(self) -> None:
        """Submit the current input text."""
        if self._text_area is None:
            return

        text = self._text_area.text.strip()
        if not text:
            return

        # Add to history
        if not self._history or self._history[-1] != text:
            self._history.append(text)
        self._history_idx = -1

        # Clear input
        self._text_area.clear()

        # Check for slash command
        cmd = parse_slash_command(text)
        if cmd:
            self.post_message(self.SlashCommandReceived(cmd))
        else:
            self.post_message(self.Submitted(text))

    # -- History navigation -------------------------------------------------

    def _history_previous(self) -> None:
        """Navigate to the previous command in history."""
        if not self._history or self._text_area is None:
            return

        if self._history_idx == -1:
            self._history_idx = len(self._history) - 1
        elif self._history_idx > 0:
            self._history_idx -= 1
        else:
            return

        self._text_area.clear()
        self._text_area.insert(self._history[self._history_idx])

    def _history_next(self) -> None:
        """Navigate to the next command in history."""
        if self._text_area is None:
            return

        if self._history_idx == -1:
            return

        if self._history_idx < len(self._history) - 1:
            self._history_idx += 1
            self._text_area.clear()
            self._text_area.insert(self._history[self._history_idx])
        else:
            self._history_idx = -1
            self._text_area.clear()

    # -- Autocomplete -------------------------------------------------------

    def _autocomplete(self) -> None:
        """File path autocomplete for Code mode."""
        if self._text_area is None:
            return

        text = self._text_area.text
        if not text:
            return

        # Find the last word (potential file path)
        words = text.rsplit(" ", 1)
        prefix = words[-1] if words else ""
        if not prefix:
            return

        # Resolve relative to cwd
        try:
            base = Path(self._cwd).resolve()
            search = base / prefix

            if search.parent.exists():
                parent = search.parent
                stem = search.name

                matches = [
                    p.name for p in parent.iterdir()
                    if p.name.startswith(stem) and not p.name.startswith(".")
                ]

                if len(matches) == 1:
                    completion = matches[0]
                    # Replace the prefix with the full match
                    if len(words) > 1:
                        new_text = words[0] + " " + str(
                            (parent / completion).relative_to(base)
                        )
                    else:
                        new_text = str(
                            (parent / completion).relative_to(base)
                        )

                    # Add trailing / for directories
                    if (parent / completion).is_dir():
                        new_text += "/"

                    self._text_area.clear()
                    self._text_area.insert(new_text)

                elif len(matches) > 1:
                    # Find common prefix
                    common = os.path.commonprefix(matches)
                    if common and len(common) > len(stem):
                        if len(words) > 1:
                            new_text = words[0] + " " + str(
                                (parent / common).relative_to(base)
                            )
                        else:
                            new_text = str(
                                (parent / common).relative_to(base)
                            )
                        self._text_area.clear()
                        self._text_area.insert(new_text)
        except (OSError, ValueError):
            pass

    # -- Public API ---------------------------------------------------------

    def set_mode(self, mode: TUIMode) -> None:
        """Update the mode indicator."""
        self.mode = mode

    def focus_input(self) -> None:
        """Focus the text input area."""
        if self._text_area:
            self._text_area.focus()

    def set_text(self, text: str) -> None:
        """Set the input text programmatically."""
        if self._text_area:
            self._text_area.clear()
            self._text_area.insert(text)

    @property
    def text(self) -> str:
        """Get the current input text."""
        if self._text_area:
            return self._text_area.text
        return ""

    @property
    def is_empty(self) -> bool:
        """Check if the input is empty."""
        return not self.text.strip()
