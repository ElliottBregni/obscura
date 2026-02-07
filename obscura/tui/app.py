"""
Obscura TUI - Terminal User Interface

A Claude Code-style interactive TUI for Obscura.
Usage: obscura tui
"""

from __future__ import annotations

import asyncio
from typing import Any

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.widgets import (
    Footer,
    Header,
    Label,
    ListItem,
    ListView,
    Static,
    TabbedContent,
    TabPane,
)

from obscura.tui.screens.chat import ChatScreen
from obscura.tui.screens.plan import PlanScreen
from obscura.tui.screens.code import CodeScreen
from obscura.tui.screens.diff import DiffScreen
from obscura.tui.screens.dashboard import DashboardScreen


class TUIApp(App):
    """Main Obscura TUI Application."""
    
    CSS = """
    Screen {
        align: center middle;
    }
    
    #sidebar {
        width: 25;
        height: 100%;
        dock: left;
        background: $surface-darken-1;
        border-right: solid $primary;
    }
    
    #content {
        width: 1fr;
        height: 100%;
    }
    
    #status-bar {
        dock: bottom;
        height: 1;
        background: $surface-darken-1;
        color: $text;
        content-align: center middle;
    }
    
    .sidebar-title {
        background: $primary;
        color: $text;
        text-align: center;
        text-style: bold;
        padding: 1;
    }
    
    .sidebar-item {
        padding: 0 1;
    }
    
    .sidebar-item:hover {
        background: $primary-darken-2;
    }
    
    TabbedContent {
        height: 100%;
    }
    """
    
    BINDINGS = [
        Binding("ctrl+c", "quit", "Quit", show=True),
        Binding("ctrl+t", "toggle_theme", "Theme", show=True),
        Binding("f1", "help", "Help", show=True),
        Binding("f2", "switch_screen('dashboard')", "Dashboard", show=True),
        Binding("f3", "switch_screen('chat')", "Chat", show=True),
        Binding("f4", "switch_screen('plan')", "Plan", show=True),
        Binding("f5", "switch_screen('code')", "Code", show=True),
        Binding("f6", "switch_screen('diff')", "Diff", show=True),
        Binding("ctrl+n", "new_agent", "New Agent", show=True),
        Binding("ctrl+m", "memory", "Memory", show=True),
    ]
    
    # Reactive state
    current_agent: reactive[str | None] = reactive(None)
    agents: reactive[list[dict]] = reactive([])
    theme: reactive[str] = reactive("dark")
    
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.title = "🧪 Obscura TUI"
        
    def compose(self) -> ComposeResult:
        """Compose the main layout."""
        yield Header(show_clock=True)
        
        with Horizontal():
            # Sidebar
            with Vertical(id="sidebar"):
                yield Label("🧪 Obscura", classes="sidebar-title")
                yield ListView(
                    ListItem(Label("📊 Dashboard (F2)", classes="sidebar-item"), id="dashboard"),
                    ListItem(Label("💬 Chat (F3)", classes="sidebar-item"), id="chat"),
                    ListItem(Label("📋 Plan (F4)", classes="sidebar-item"), id="plan"),
                    ListItem(Label("📝 Code (F5)", classes="sidebar-item"), id="code"),
                    ListItem(Label("🔍 Diff (F6)", classes="sidebar-item"), id="diff"),
                    ListItem(Label("⚙️  Settings", classes="sidebar-item"), id="settings"),
                )
            
            # Main content area
            with Vertical(id="content"):
                yield TabbedContent(
                    TabPane("Dashboard", DashboardScreen(), id="dashboard"),
                    TabPane("Chat", ChatScreen(), id="chat"),
                    TabPane("Plan", PlanScreen(), id="plan"),
                    TabPane("Code", CodeScreen(), id="code"),
                    TabPane("Diff", DiffScreen(), id="diff"),
                )
        
        # Status bar
        yield Static(
            " Ready | Press F1 for help | Ctrl+C to quit",
            id="status-bar"
        )
        
        yield Footer()
    
    def on_mount(self) -> None:
        """Called when app mounts."""
        self.notify("Welcome to Obscura TUI!", severity="information")
        self.push_screen("dashboard")
    
    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Handle sidebar navigation."""
        screen_id = event.item.id
        if screen_id:
            self.switch_screen(screen_id)
    
    def action_toggle_theme(self) -> None:
        """Toggle between dark and light theme."""
        self.theme = "light" if self.theme == "dark" else "dark"
        self.dark = self.theme == "dark"
        self.notify(f"Theme: {self.theme}", severity="information")
    
    def action_new_agent(self) -> None:
        """Open new agent dialog."""
        self.notify("New Agent dialog (coming soon)", severity="warning")
    
    def action_memory(self) -> None:
        """Open memory browser."""
        self.notify("Memory browser (coming soon)", severity="warning")
    
    def action_help(self) -> None:
        """Show help."""
        help_text = """
        # Obscura TUI Help

        ## Navigation
        - F2: Dashboard
        - F3: Chat
        - F4: Plan
        - F5: Code
        - F6: Diff
        - Tab: Next widget
        - Esc: Back

        ## Actions
        - Ctrl+N: New agent
        - Ctrl+M: Memory browser
        - Ctrl+T: Toggle theme
        - Ctrl+C: Quit
        """
        self.notify(help_text, severity="information", timeout=10)
    
    def switch_screen(self, screen_name: str) -> None:
        """Switch to a different screen."""
        tabbed = self.query_one(TabbedContent)
        tabbed.active = screen_name
        self.update_status(f"Active: {screen_name}")
    
    def update_status(self, message: str) -> None:
        """Update status bar."""
        status = self.query_one("#status-bar", Static)
        status.update(f" {message} | Press F1 for help | Ctrl+C to quit")
    
    def watch_current_agent(self, agent_id: str | None) -> None:
        """React to agent selection change."""
        if agent_id:
            self.update_status(f"Agent: {agent_id}")
        else:
            self.update_status("Ready")


def main():
    """Entry point for TUI."""
    app = TUIApp()
    app.run()


if __name__ == "__main__":
    main()
