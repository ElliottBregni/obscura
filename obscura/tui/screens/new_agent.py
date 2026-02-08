"""New Agent Dialog Screen"""

from textual.screen import ModalScreen
from textual.containers import Vertical, Horizontal
from textual.widgets import (
    Input,
    Button,
    Label,
    Select,
    Static,
)
from textual.binding import Binding

from obscura.tui.client import TUIClient


class NewAgentScreen(ModalScreen):
    """Modal dialog for creating a new agent."""
    
    CSS = """
    NewAgentScreen {
        align: center middle;
    }
    
    #dialog {
        background: $surface;
        border: solid $primary;
        padding: 1 2;
        width: 60;
        height: auto;
    }
    
    #title {
        text-align: center;
        text-style: bold;
        color: $primary;
        margin-bottom: 1;
    }
    
    Input {
        margin: 1 0;
    }
    
    Select {
        margin: 1 0;
    }
    
    #buttons {
        height: auto;
        margin-top: 1;
        align: center middle;
    }
    
    Button {
        margin: 0 1;
    }
    
    .error {
        color: $error;
        text-align: center;
    }
    """
    
    BINDINGS = [
        Binding("escape", "dismiss", "Cancel", show=True),
    ]
    
    def compose(self):
        """Compose the dialog."""
        with Vertical(id="dialog"):
            yield Label("➕ Create New Agent", id="title")
            
            yield Label("Name:")
            yield Input(placeholder="e.g., code-reviewer", id="name-input")
            
            yield Label("Model:")
            yield Select(
                [("Claude", "claude"), ("Copilot", "copilot")],
                value="claude",
                id="model-select"
            )
            
            yield Label("System Prompt (optional):")
            yield Input(
                placeholder="You are a helpful assistant...",
                id="prompt-input"
            )
            
            with Horizontal(id="buttons"):
                yield Button("Create", id="btn-create", variant="success")
                yield Button("Cancel", id="btn-cancel", variant="default")
    
    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle button presses."""
        if event.button.id == "btn-create":
            self.run_worker(self.create_agent())
        else:
            self.dismiss()
    
    async def create_agent(self):
        """Create the agent via API."""
        name_input = self.query_one("#name-input", Input)
        model_select = self.query_one("#model-select", Select)
        prompt_input = self.query_one("#prompt-input", Input)
        
        name = name_input.value.strip()
        model = model_select.value
        prompt = prompt_input.value.strip() or None
        
        if not name:
            self.app.notify("Name is required", severity="error")
            return
        
        try:
            async with TUIClient() as client:
                kwargs = {}
                if prompt:
                    kwargs["system_prompt"] = prompt
                
                agent = await client.spawn_agent(name, model, **kwargs)
                
                self.app.notify(
                    f"Created agent: {agent.get('name', 'Unknown')}",
                    severity="success"
                )
                self.dismiss(result=agent)
                
        except Exception as e:
            self.app.notify(f"Failed to create agent: {e}", severity="error")
