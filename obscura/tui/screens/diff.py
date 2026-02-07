"""Diff Screen - Side-by-side Diff Viewer"""

from textual.screen import Screen
from textual.containers import Vertical, Horizontal
from textual.widgets import (
    Static,
    Button,
    Label,
    RichLog,
    Checkbox,
)
from textual.reactive import reactive
from textual.binding import Binding
from rich.panel import Panel
from rich.text import Text


class DiffScreen(Screen):
    """Side-by-side diff viewer."""
    
    CSS = """
    DiffScreen {
        padding: 1;
    }
    
    #diff-header {
        height: auto;
        margin-bottom: 1;
    }
    
    #diff-container {
        height: 1fr;
    }
    
    #original-panel {
        width: 50%;
        height: 100%;
        border: solid $error;
        padding: 1;
    }
    
    #modified-panel {
        width: 50%;
        height: 100%;
        border: solid $success;
        padding: 1;
    }
    
    .diff-line-removed {
        background: $error-darken-2;
        color: $text;
    }
    
    .diff-line-added {
        background: $success-darken-2;
        color: $text;
    }
    
    .diff-line-context {
        color: $text;
    }
    
    #diff-actions {
        height: auto;
        margin-top: 1;
        text-align: center;
    }
    """
    
    BINDINGS = [
        Binding("a", "accept", "Accept", show=True),
        Binding("r", "reject", "Reject", show=True),
        Binding("n", "next_diff", "Next", show=True),
        Binding("p", "prev_diff", "Prev", show=True),
    ]
    
    original_content: reactive[str] = reactive("")
    modified_content: reactive[str] = reactive("")
    filename: reactive[str] = reactive("unknown")
    
    def compose(self):
        """Compose the diff screen."""
        with Vertical():
            # Header
            with Horizontal(id="diff-header"):
                yield Label("🔍 Diff View", classes="title")
                yield Label("src/auth/middleware.py", id="diff-filename")
                yield Checkbox("Show context", id="show-context", value=True)
            
            # Diff panels
            with Horizontal(id="diff-container"):
                with Vertical(id="original-panel"):
                    yield Label("📄 Original", classes="panel-title")
                    yield RichLog(id="original-content", wrap=False)
                
                with Vertical(id="modified-panel"):
                    yield Label("✏️  Modified", classes="panel-title")
                    yield RichLog(id="modified-content", wrap=False)
            
            # Actions
            with Horizontal(id="diff-actions"):
                yield Button("✅ Accept All", id="btn-accept-all", variant="success")
                yield Button("❌ Reject All", id="btn-reject-all", variant="error")
                yield Button("✓ Accept", id="btn-accept", variant="primary")
                yield Button("✗ Reject", id="btn-reject", variant="default")
                yield Button("⬅ Previous", id="btn-prev", variant="default")
                yield Button("Next ➡", id="btn-next", variant="default")
    
    def on_mount(self):
        """Set up the diff screen."""
        self.load_mock_diff()
    
    def load_mock_diff(self):
        """Load mock diff data for demonstration."""
        self.filename = "src/auth/middleware.py"
        
        self.original_content = '''"""Authentication middleware."""

def check_auth(token):
    if not token:
        return False
    
    # Validate token
    query = f"SELECT * FROM users WHERE id = {user_id}"
    result = db.execute(query)
    
    return result is not None'''
        
        self.modified_content = '''"""Authentication middleware."""

import jwt
from datetime import datetime

def check_auth(token):
    if not token:
        return False
    
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        return payload
    except jwt.ExpiredSignatureError:
        return None
    
    # Validate token securely
    query = "SELECT * FROM users WHERE id = ?"
    result = db.execute(query, (user_id,))
    
    return result is not None'''
        
        self.display_diff()
    
    def display_diff(self):
        """Display the diff."""
        # Update filename
        self.query_one("#diff-filename", Label).update(self.filename)
        
        # Display original
        original_log = self.query_one("#original-content", RichLog)
        original_log.clear()
        
        original_lines = self.original_content.split("\n")
        for i, line in enumerate(original_lines, 1):
            # Simple diff highlighting (would use actual diff algorithm in production)
            if i in [6, 7]:  # Lines that were changed
                text = Text(f"{i:3d} - {line}", style="red")
            else:
                text = Text(f"{i:3d} │ {line}")
            original_log.write(text)
        
        # Display modified
        modified_log = self.query_one("#modified-content", RichLog)
        modified_log.clear()
        
        modified_lines = self.modified_content.split("\n")
        for i, line in enumerate(modified_lines, 1):
            # Simple diff highlighting
            if i in [3, 4, 10, 11, 12, 13, 14, 17]:  # Added/changed lines
                text = Text(f"{i:3d} + {line}", style="green")
            else:
                text = Text(f"{i:3d} │ {line}")
            modified_log.write(text)
    
    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle button presses."""
        if event.button.id == "btn-accept-all":
            self.app.notify("Accepted all changes!", severity="success")
        elif event.button.id == "btn-reject-all":
            self.app.notify("Rejected all changes!", severity="warning")
        elif event.button.id == "btn-accept":
            self.app.notify("Accepted current change", severity="success")
        elif event.button.id == "btn-reject":
            self.app.notify("Rejected current change", severity="warning")
        elif event.button.id == "btn-next":
            self.action_next_diff()
        elif event.button.id == "btn-prev":
            self.action_prev_diff()
    
    def action_accept(self) -> None:
        """Accept current diff."""
        self.app.notify("Change accepted", severity="success")
    
    def action_reject(self) -> None:
        """Reject current diff."""
        self.app.notify("Change rejected", severity="warning")
    
    def action_next_diff(self) -> None:
        """Go to next diff."""
        self.app.notify("Next diff", severity="information")
    
    def action_prev_diff(self) -> None:
        """Go to previous diff."""
        self.app.notify("Previous diff", severity="information")
