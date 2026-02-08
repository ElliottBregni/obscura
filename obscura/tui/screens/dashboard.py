"""Dashboard Screen - Agent Overview with real API"""

from textual.screen import Screen
from textual.containers import Vertical, Horizontal, Grid
from textual.widgets import (
    Static,
    DataTable,
    Button,
    Label,
)
from textual.reactive import reactive
from textual.binding import Binding
from textual.worker import Worker

from obscura.tui.client import TUIClient


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
    
    .loading {
        text-align: center;
        color: $text-muted;
    }
    """
    
    BINDINGS = [
        Binding("r", "refresh", "Refresh", show=True),
        Binding("n", "new_agent", "New Agent", show=True),
    ]
    
    agents: reactive[list] = reactive([])
    stats: reactive[dict] = reactive({})
    loading: reactive[bool] = reactive(False)
    
    def compose(self):
        """Compose the dashboard."""
        with Vertical():
            # Stats cards
            with Grid(id="stats-grid"):
                with Vertical(classes="stat-card"):
                    yield Label("-", classes="stat-value", id="stat-active")
                    yield Label("Active Agents", classes="stat-label")
                
                with Vertical(classes="stat-card"):
                    yield Label("-", classes="stat-value", id="stat-running")
                    yield Label("Running Tasks", classes="stat-label")
                
                with Vertical(classes="stat-card"):
                    yield Label("-", classes="stat-value", id="stat-waiting")
                    yield Label("Waiting", classes="stat-label")
                
                with Vertical(classes="stat-card"):
                    yield Label("-", classes="stat-value", id="stat-memory")
                    yield Label("Memory Entries", classes="stat-label")
            
            # Actions
            with Horizontal(id="actions"):
                yield Button("➕ New Agent", id="btn-new", variant="primary")
                yield Button("🔄 Refresh", id="btn-refresh", variant="default")
                yield Button("⏹ Stop All", id="btn-stop-all", variant="error")
            
            # Agents table
            yield Label("Active Agents", classes="section-title")
            yield DataTable(id="agents-table")
    
    def on_mount(self):
        """Set up the dashboard."""
        table = self.query_one("#agents-table", DataTable)
        table.add_columns("ID", "Name", "Status", "Created", "Actions")
        table.cursor_type = "row"
        
        # Load initial data
        self.load_data()
    
    async def load_data(self):
        """Load agents and stats from API."""
        self.loading = True
        self.app.notify("Loading...", severity="information", timeout=1)
        
        try:
            async with TUIClient() as client:
                # Fetch data
                self.agents = await client.list_agents()
                self.stats = await client.get_stats()
                
        except Exception as e:
            self.app.notify(f"Error loading data: {e}", severity="error")
            self.agents = []
            self.stats = {}
        
        self.loading = False
        self.update_display()
    
    def update_display(self):
        """Update the display with current data."""
        # Update stats
        self.query_one("#stat-active", Label).update(str(self.stats.get("active", 0)))
        self.query_one("#stat-running", Label).update(str(self.stats.get("running", 0)))
        self.query_one("#stat-waiting", Label).update(str(self.stats.get("waiting", 0)))
        self.query_one("#stat-memory", Label).update(str(self.stats.get("memory", 0)))
        
        # Update table
        table = self.query_one("#agents-table", DataTable)
        table.clear()
        
        for agent in self.agents:
            status = agent.get("status", "UNKNOWN")
            status_emoji = {
                "RUNNING": "🟢",
                "WAITING": "🟡",
                "ERROR": "🔴",
                "COMPLETED": "✅",
            }.get(status, "⚪")
            
            created = agent.get("created_at", "")[:10]  # Just date
            
            table.add_row(
                agent.get("agent_id", "")[:8],
                agent.get("name", "Unnamed"),
                f"{status_emoji} {status}",
                created,
                "[Stop]",
                key=agent.get("agent_id", "")
            )
    
    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle button presses."""
        if event.button.id == "btn-new":
            self.action_new_agent()
        elif event.button.id == "btn-refresh":
            self.run_worker(self.load_data())
        elif event.button.id == "btn-stop-all":
            self.run_worker(self.stop_all_agents())
    
    async def stop_all_agents(self):
        """Stop all running agents."""
        if not self.agents:
            self.app.notify("No agents to stop", severity="information")
            return
        
        self.app.notify("Stopping all agents...", severity="warning")
        
        try:
            async with TUIClient() as client:
                for agent in self.agents:
                    try:
                        await client.stop_agent(agent["agent_id"])
                    except Exception:
                        pass  # Agent might already be stopped
            
            self.app.notify("All agents stopped", severity="information")
            await self.load_data()
            
        except Exception as e:
            self.app.notify(f"Error stopping agents: {e}", severity="error")
    
    def action_refresh(self):
        """Refresh dashboard data."""
        self.run_worker(self.load_data())
    
    def action_new_agent(self):
        """Open new agent dialog."""
        self.app.push_screen("new_agent")
