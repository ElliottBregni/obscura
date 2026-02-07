"""Dashboard Screen - Agent Overview"""

from textual.screen import Screen
from textual.containers import Vertical, Horizontal, Grid
from textual.widgets import (
    Static,
    DataTable,
    Button,
    Label,
    ProgressBar,
    Rule,
)
from textual.reactive import reactive
from textual.binding import Binding


class DashboardScreen(Screen):
    """Dashboard showing agent overview and stats."""
    
    CSS = """
    DashboardScreen {
        padding: 1;
    }
    
    #stats-grid {
        grid-size: 4;
        grid-gutter: 1;
        height: auto;
    }
    
    .stat-card {
        background: $surface-darken-1;
        border: solid $primary;
        padding: 1;
        text-align: center;
    }
    
    .stat-value {
        text-style: bold;
        text-align: center;
        color: $primary;
    }
    
    .stat-label {
        text-align: center;
        color: $text-muted;
    }
    
    #agents-table {
        height: 1fr;
        border: solid $primary;
    }
    
    #actions {
        height: auto;
        margin: 1 0;
    }
    """
    
    BINDINGS = [
        Binding("r", "refresh", "Refresh", show=True),
        Binding("n", "new_agent", "New Agent", show=True),
    ]
    
    agents: reactive[list] = reactive([])
    stats: reactive[dict] = reactive({})
    
    def compose(self):
        """Compose the dashboard."""
        with Vertical():
            # Stats cards
            with Grid(id="stats-grid"):
                with Vertical(classes="stat-card"):
                    yield Label("0", classes="stat-value", id="stat-active")
                    yield Label("Active Agents", classes="stat-label")
                
                with Vertical(classes="stat-card"):
                    yield Label("0", classes="stat-value", id="stat-running")
                    yield Label("Running Tasks", classes="stat-label")
                
                with Vertical(classes="stat-card"):
                    yield Label("0", classes="stat-value", id="stat-completed")
                    yield Label("Completed", classes="stat-label")
                
                with Vertical(classes="stat-card"):
                    yield Label("0", classes="stat-value", id="stat-memory")
                    yield Label("Memory Entries", classes="stat-label")
            
            yield Rule()
            
            # Actions
            with Horizontal(id="actions"):
                yield Button("➕ New Agent", id="btn-new", variant="primary")
                yield Button("🔄 Refresh", id="btn-refresh", variant="default")
                yield Button("⏹ Stop All", id="btn-stop-all", variant="error")
            
            yield Rule()
            
            # Agents table
            yield Label("Active Agents", classes="section-title")
            yield DataTable(id="agents-table")
    
    def on_mount(self):
        """Set up the dashboard."""
        table = self.query_one("#agents-table", DataTable)
        table.add_columns("ID", "Name", "Model", "Status", "Runtime", "Actions")
        table.cursor_type = "row"
        
        # Load initial data
        self.load_data()
    
    def load_data(self):
        """Load agents and stats."""
        # TODO: Connect to actual API
        self.app.notify("Loading dashboard data...", severity="information")
        
        # Mock data for now
        self.agents = [
            {"id": "agent-1", "name": "code-reviewer", "model": "claude", "status": "running", "runtime": "5m"},
            {"id": "agent-2", "name": "doc-writer", "model": "claude", "status": "waiting", "runtime": "2m"},
        ]
        
        self.stats = {
            "active": len(self.agents),
            "running": sum(1 for a in self.agents if a["status"] == "running"),
            "completed": 12,
            "memory": 45,
        }
        
        self.update_display()
    
    def update_display(self):
        """Update the display with current data."""
        # Update stats
        self.query_one("#stat-active", Label).update(str(self.stats.get("active", 0)))
        self.query_one("#stat-running", Label).update(str(self.stats.get("running", 0)))
        self.query_one("#stat-completed", Label).update(str(self.stats.get("completed", 0)))
        self.query_one("#stat-memory", Label).update(str(self.stats.get("memory", 0)))
        
        # Update table
        table = self.query_one("#agents-table", DataTable)
        table.clear()
        
        for agent in self.agents:
            status_emoji = {
                "running": "🟢",
                "waiting": "🟡",
                "error": "🔴",
                "completed": "✅",
            }.get(agent["status"], "⚪")
            
            table.add_row(
                agent["id"][:8],
                agent["name"],
                agent["model"],
                f"{status_emoji} {agent['status']}",
                agent["runtime"],
                "[View] [Stop]",
                key=agent["id"]
            )
    
    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle button presses."""
        if event.button.id == "btn-new":
            self.action_new_agent()
        elif event.button.id == "btn-refresh":
            self.load_data()
        elif event.button.id == "btn-stop-all":
            self.app.notify("Stopping all agents...", severity="warning")
    
    def action_refresh(self):
        """Refresh dashboard data."""
        self.load_data()
    
    def action_new_agent(self):
        """Open new agent dialog."""
        self.app.notify("New agent dialog (implement me)", severity="information")
