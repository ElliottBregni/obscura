"""Chat Screen - Interactive Agent Chat"""

from textual.screen import Screen
from textual.containers import Vertical, Horizontal
from textual.widgets import (
    Static,
    Input,
    Button,
    Label,
    RichLog,
    Select,
    LoadingIndicator,
)
from textual.reactive import reactive
from textual.binding import Binding
from rich.text import Text
from rich.panel import Panel


class ChatScreen(Screen):
    """Interactive chat interface with agents."""
    
    CSS = """
    ChatScreen {
        padding: 0;
    }
    
    #chat-container {
        height: 100%;
        padding: 1;
    }
    
    #agent-selector {
        height: auto;
        margin-bottom: 1;
    }
    
    #chat-log {
        height: 1fr;
        border: solid $primary;
        padding: 1;
        background: $surface-darken-1;
    }
    
    .message-user {
        color: $text;
        background: $primary-darken-2;
        padding: 1;
        margin: 1 0;
        border-left: solid $primary;
    }
    
    .message-agent {
        color: $text;
        background: $surface;
        padding: 1;
        margin: 1 0;
        border-left: solid $success;
    }
    
    .message-system {
        color: $text-muted;
        text-style: italic;
        text-align: center;
        margin: 1 0;
    }
    
    #input-area {
        height: auto;
        margin-top: 1;
    }
    
    #message-input {
        width: 1fr;
    }
    
    #send-button {
        width: auto;
    }
    
    #loading {
        display: none;
        height: auto;
        text-align: center;
        color: $primary;
    }
    
    #loading.active {
        display: block;
    }
    """
    
    BINDINGS = [
        Binding("ctrl+s", "send", "Send", show=True),
        Binding("ctrl+l", "clear", "Clear", show=True),
        Binding("up", "history_prev", "Prev", show=False),
        Binding("down", "history_next", "Next", show=False),
    ]
    
    messages: reactive[list] = reactive([])
    current_agent: reactive[str | None] = reactive(None)
    is_loading: reactive[bool] = reactive(False)
    
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.message_history = []
        self.history_index = 0
    
    def compose(self):
        """Compose the chat screen."""
        with Vertical(id="chat-container"):
            # Agent selector
            with Horizontal(id="agent-selector"):
                yield Label("Agent:")
                yield Select(
                    [("Select agent...", None), ("code-reviewer", "agent-1"), ("doc-writer", "agent-2")],
                    id="agent-select",
                    value=None,
                )
                yield Button("➕ New", id="btn-new-agent", variant="primary")
            
            # Chat log
            yield RichLog(id="chat-log", wrap=True, highlight=True)
            
            # Loading indicator
            with Vertical(id="loading"):
                yield LoadingIndicator()
                yield Label("Agent is thinking...")
            
            # Input area
            with Horizontal(id="input-area"):
                yield Input(
                    placeholder="Type your message... (Ctrl+S to send)",
                    id="message-input",
                )
                yield Button("Send", id="send-button", variant="primary")
    
    def on_mount(self):
        """Set up the chat screen."""
        self.add_system_message("Welcome to Obscura Chat!")
        self.add_system_message("Select an agent to start chatting.")
    
    def add_message(self, role: str, content: str) -> None:
        """Add a message to the chat."""
        log = self.query_one("#chat-log", RichLog)
        
        if role == "user":
            text = Text(f"You: {content}", style="bold cyan")
            log.write(Panel(text, border_style="cyan"))
        elif role == "agent":
            text = Text(f"Agent: {content}", style="bold green")
            log.write(Panel(text, border_style="green"))
        elif role == "system":
            text = Text(content, style="dim italic")
            log.write(text)
        
        self.messages.append({"role": role, "content": content})
    
    def add_system_message(self, content: str) -> None:
        """Add a system message."""
        self.add_message("system", content)
    
    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle button presses."""
        if event.button.id == "send-button":
            self.action_send()
        elif event.button.id == "btn-new-agent":
            self.app.notify("New agent dialog", severity="information")
    
    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Handle input submission."""
        if event.input.id == "message-input":
            self.action_send()
    
    def action_send(self) -> None:
        """Send the current message."""
        input_widget = self.query_one("#message-input", Input)
        message = input_widget.value.strip()
        
        if not message:
            return
        
        if not self.current_agent:
            self.app.notify("Please select an agent first!", severity="error")
            return
        
        # Add user message
        self.add_message("user", message)
        input_widget.value = ""
        
        # Store in history
        self.message_history.append(message)
        self.history_index = len(self.message_history)
        
        # Simulate agent response (TODO: connect to real API)
        self.send_to_agent(message)
    
    def send_to_agent(self, message: str) -> None:
        """Send message to agent and handle response."""
        self.is_loading = True
        
        # TODO: Connect to actual Obscura API
        # For now, simulate async response
        self.app.notify(f"Sending to {self.current_agent}...", severity="information")
        
        # Mock response
        import asyncio
        asyncio.create_task(self._mock_response())
    
    async def _mock_response(self):
        """Mock agent response for testing."""
        await asyncio.sleep(2)
        self.is_loading = False
        self.add_message("agent", "This is a mock response. Connect to real API for actual agent responses.")
    
    def watch_is_loading(self, loading: bool) -> None:
        """Show/hide loading indicator."""
        loading_widget = self.query_one("#loading", Vertical)
        if loading:
            loading_widget.add_class("active")
        else:
            loading_widget.remove_class("active")
    
    def on_select_changed(self, event: Select.Changed) -> None:
        """Handle agent selection."""
        if event.select.id == "agent-select":
            self.current_agent = event.value
            if self.current_agent:
                self.add_system_message(f"Switched to agent: {self.current_agent}")
                self.app.current_agent = self.current_agent
    
    def action_clear(self) -> None:
        """Clear chat history."""
        log = self.query_one("#chat-log", RichLog)
        log.clear()
        self.messages.clear()
        self.add_system_message("Chat history cleared.")
    
    def action_history_prev(self) -> None:
        """Navigate to previous message in history."""
        if self.history_index > 0:
            self.history_index -= 1
            input_widget = self.query_one("#message-input", Input)
            input_widget.value = self.message_history[self.history_index]
    
    def action_history_next(self) -> None:
        """Navigate to next message in history."""
        if self.history_index < len(self.message_history) - 1:
            self.history_index += 1
            input_widget = self.query_one("#message-input", Input)
            input_widget.value = self.message_history[self.history_index]
