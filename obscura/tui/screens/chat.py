"""Chat Screen - Interactive agent chat with real API"""

from textual.screen import Screen
from textual.containers import Vertical, Horizontal
from textual.widgets import (
    Input,
    Button,
    Label,
    Log,
    Select,
    Static,
)
from textual.reactive import reactive
from textual.binding import Binding

from obscura.tui.client import TUIClient


class ChatScreen(Screen):
    """Interactive chat with agents."""
    
    CSS = """
    ChatScreen {
        padding: 0;
    }
    
    #header {
        height: auto;
        padding: 0 1;
        background: $surface-darken-1;
        border-bottom: solid $primary;
    }
    
    #agent-select {
        width: 30;
    }
    
    #chat-log {
        height: 1fr;
        border: solid $primary;
        margin: 1;
        padding: 0;
    }
    
    #input-area {
        height: auto;
        padding: 0 1 1 1;
    }
    
    #message-input {
        width: 1fr;
    }
    
    #send-btn {
        width: auto;
        margin-left: 1;
    }
    
    .message-user {
        color: $primary;
        margin: 0 0 0 0;
    }
    
    .message-agent {
        color: $text;
        margin: 0 0 1 0;
    }
    
    .message-system {
        color: $text-muted;
        text-style: italic;
    }
    """
    
    BINDINGS = [
        Binding("enter", "send_message", "Send", show=True),
        Binding("ctrl+r", "refresh_agents", "Refresh Agents", show=True),
    ]
    
    current_agent: reactive[str | None] = reactive(None)
    messages: reactive[list] = reactive([])
    agents: reactive[list] = reactive([])
    
    def compose(self):
        """Compose the chat screen."""
        with Vertical():
            # Header with agent selector
            with Horizontal(id="header"):
                yield Label("Agent: ")
                yield Select([], id="agent-select", prompt="Select agent...")
                yield Button("🔄", id="btn-refresh", variant="default")
            
            # Chat log
            yield Log(id="chat-log", highlight=True)
            
            # Input area
            with Horizontal(id="input-area"):
                yield Input(placeholder="Type a message...", id="message-input")
                yield Button("Send", id="send-btn", variant="primary")
    
    def on_mount(self):
        """Set up the chat screen."""
        self.refresh_agents()
        self.add_message("system", "Welcome to Obscura Chat!")
        self.add_message("system", "Select an agent to start chatting.")
    
    def watch_agents(self, agents: list):
        """Update agent selector when agents change."""
        select = self.query_one("#agent-select", Select)
        options = [(f"{a.get('name', 'Unnamed')} ({a.get('agent_id', '')[:6]}...)", a.get('agent_id')) 
                   for a in agents]
        select.set_options(options)
    
    def add_message(self, role: str, content: str):
        """Add a message to the chat log."""
        log = self.query_one("#chat-log", Log)
        
        if role == "user":
            prefix = "You: "
            style = "message-user"
        elif role == "agent":
            prefix = "Agent: "
            style = "message-agent"
        else:
            prefix = ""
            style = "message-system"
        
        log.write_line(f"{prefix}{content}")
    
    async def refresh_agents(self):
        """Refresh the list of agents."""
        try:
            async with TUIClient() as client:
                self.agents = await client.list_agents()
                
                if not self.agents:
                    self.add_message("system", "No agents found. Create one from the dashboard.")
                else:
                    self.add_message("system", f"Found {len(self.agents)} agent(s)")
                    
        except Exception as e:
            self.add_message("system", f"Error loading agents: {e}")
    
    def action_refresh_agents(self):
        """Refresh agents action."""
        self.run_worker(self.refresh_agents())
    
    def on_select_changed(self, event: Select.Changed) -> None:
        """Handle agent selection."""
        if event.value:
            self.current_agent = event.value
            self.add_message("system", f"Selected agent: {event.value[:8]}...")
    
    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle button presses."""
        if event.button.id == "btn-refresh":
            self.run_worker(self.refresh_agents())
        elif event.button.id == "send-btn":
            self.action_send_message()
    
    def action_send_message(self):
        """Send a message to the current agent."""
        if not self.current_agent:
            self.app.notify("Please select an agent first", severity="error")
            return
        
        input_widget = self.query_one("#message-input", Input)
        message = input_widget.value.strip()
        
        if not message:
            return
        
        input_widget.value = ""
        self.add_message("user", message)
        
        # Send to agent
        self.run_worker(self.send_to_agent(message))
    
    async def send_to_agent(self, message: str):
        """Send message to agent via API."""
        try:
            self.add_message("system", "Thinking...")
            
            async with TUIClient() as client:
                result = await client.run_task(self.current_agent, message)
                
                # Remove "Thinking..." and add response
                # (In real implementation, we'd track and remove the thinking message)
                response = result.get("response", "No response")
                self.add_message("agent", response)
                
        except Exception as e:
            self.add_message("system", f"Error: {e}")
