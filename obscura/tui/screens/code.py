"""Code Screen - File Browser and Editor"""

from textual.screen import Screen
from textual.containers import Vertical, Horizontal
from textual.widgets import (
    Static,
    Tree,
    Label,
    Button,
    Input,
    RichLog,
)
from textual.reactive import reactive
from textual.binding import Binding
from rich.syntax import Syntax


class CodeScreen(Screen):
    """Code browser and editor screen."""
    
    CSS = """
    CodeScreen {
        padding: 0;
    }
    
    #code-container {
        height: 100%;
    }
    
    #file-tree {
        width: 30;
        height: 100%;
        dock: left;
        border-right: solid $primary;
        padding: 1;
    }
    
    #editor-area {
        height: 100%;
        padding: 1;
    }
    
    #editor-toolbar {
        height: auto;
        margin-bottom: 1;
    }
    
    #code-display {
        height: 1fr;
        border: solid $primary;
        padding: 1;
        background: $surface-darken-1;
    }
    
    #file-info {
        height: auto;
        text-style: dim;
    }
    """
    
    BINDINGS = [
        Binding("ctrl+o", "open_file", "Open", show=True),
        Binding("ctrl+s", "save_file", "Save", show=True),
        Binding("ctrl+f", "find", "Find", show=True),
    ]
    
    current_file: reactive[str | None] = reactive(None)
    file_content: reactive[str] = reactive("")
    
    def compose(self):
        """Compose the code screen."""
        with Horizontal(id="code-container"):
            # File tree sidebar
            with Vertical(id="file-tree"):
                yield Label("📁 Files", classes="sidebar-title")
                yield Tree(".", id="file-tree-widget")
            
            # Editor area
            with Vertical(id="editor-area"):
                # Toolbar
                with Horizontal(id="editor-toolbar"):
                    yield Button("📂 Open", id="btn-open", variant="primary")
                    yield Button("💾 Save", id="btn-save", variant="success")
                    yield Button("🔍 Find", id="btn-find", variant="default")
                    yield Input(placeholder="Search...", id="search-input")
                
                # File info
                yield Label("No file open", id="file-info")
                
                # Code display
                yield RichLog(id="code-display", wrap=False, highlight=True)
    
    def on_mount(self):
        """Set up the code screen."""
        self.build_file_tree()
    
    def build_file_tree(self):
        """Build the file tree."""
        tree = self.query_one("#file-tree-widget", Tree)
        tree.clear()
        
        # Mock file structure
        root = tree.root
        root.add("src/")
        root.add("tests/")
        root.add("docs/")
        
        src = root.add("src/", expand=True)
        src.add("sdk/")
        src.add("auth/")
        src.add("server.py")
        src.add("client.py")
        
        sdk = src.add("sdk/", expand=True)
        sdk.add("agent.py")
        sdk.add("memory.py")
        sdk.add("server.py")
    
    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        """Handle file selection."""
        label = str(event.node.label)
        if not label.endswith("/"):
            self.load_file(label)
    
    def load_file(self, filepath: str):
        """Load and display a file."""
        self.current_file = filepath
        
        # Mock file content
        if filepath == "server.py":
            self.file_content = '''"""SDK Server - FastAPI HTTP API"""

from fastapi import FastAPI, Depends
from sdk.agent import Agent
from sdk.memory import MemoryStore

app = FastAPI()

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.post("/api/v1/agents")
async def spawn_agent(body: dict):
    agent = Agent.create(body)
    return {"agent_id": agent.id}
'''
        else:
            self.file_content = f"# Content of {filepath}\n\n# TODO: Load actual file content"
        
        self.display_code()
    
    def display_code(self):
        """Display the current file content."""
        # Update file info
        info = self.query_one("#file-info", Label)
        if self.current_file:
            info.update(f"📄 {self.current_file} | Python | 3.4 KB")
        else:
            info.update("No file open")
        
        # Display code with syntax highlighting
        log = self.query_one("#code-display", RichLog)
        log.clear()
        
        if self.file_content:
            # Add line numbers
            lines = self.file_content.split("\n")
            for i, line in enumerate(lines, 1):
                log.write(f"{i:4d} │ {line}")
    
    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle button presses."""
        if event.button.id == "btn-open":
            self.action_open_file()
        elif event.button.id == "btn-save":
            self.app.notify("File saved!", severity="success")
        elif event.button.id == "btn-find":
            self.action_find()
    
    def action_open_file(self) -> None:
        """Open file dialog."""
        self.app.notify("Open file dialog", severity="information")
    
    def action_find(self) -> None:
        """Find in file."""
        self.app.notify("Find dialog", severity="information")
    
    def watch_current_file(self, filepath: str | None) -> None:
        """React to file change."""
        if filepath:
            self.app.update_status(f"Editing: {filepath}")
