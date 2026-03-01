"""AttentionModal — Textual modal for agent attention requests.

Shown when an agent requests user input via the :class:`InteractionBus`.
Displays the agent name, message, and action buttons.  The user's
choice is routed back through the bus.

Usage::

    from obscura.tui.widgets.attention_modal import AttentionModal

    modal = AttentionModal(request)
    chosen = await app.push_screen(modal)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static

if TYPE_CHECKING:
    from obscura.agent.interaction import AttentionRequest


class AttentionModal(ModalScreen[str]):
    """Modal dialog for an agent attention request.

    Returns the selected action string when dismissed.
    """

    DEFAULT_CSS = """
    AttentionModal {
        align: center middle;
    }

    AttentionModal > Vertical {
        width: 60;
        max-height: 20;
        padding: 1 2;
        background: $surface;
        border: thick $accent;
    }

    AttentionModal .agent-name {
        text-style: bold;
        color: $accent;
        margin-bottom: 1;
    }

    AttentionModal .message-text {
        margin-bottom: 1;
    }

    AttentionModal .priority-tag {
        margin-bottom: 1;
        color: $warning;
    }

    AttentionModal Input {
        margin-top: 1;
    }

    AttentionModal Button {
        margin: 0 1;
    }
    """

    def __init__(self, request: AttentionRequest) -> None:
        super().__init__()
        self._request = request

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(
                f"[{self._request.agent_name}]",
                classes="agent-name",
            )
            yield Static(
                self._request.message,
                classes="message-text",
            )
            yield Label(
                f"Priority: {self._request.priority.value}",
                classes="priority-tag",
            )
            for action in self._request.actions:
                yield Button(action, id=f"action-{action}")
            yield Input(placeholder="Or type a response...", id="free-text")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle action button clicks."""
        action = event.button.id
        if action and action.startswith("action-"):
            self.dismiss(action.removeprefix("action-"))
        else:
            self.dismiss(str(event.button.label))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Handle free-text submission."""
        text = event.value.strip()
        if text:
            self.dismiss(text)
