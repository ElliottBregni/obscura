"""Plan Screen - Task Planning and Execution"""

from textual.screen import Screen
from textual.containers import Vertical, Horizontal
from textual.widgets import (
    Static,
    Tree,
    Button,
    Label,
    ProgressBar,
    Checkbox,
    Input,
)
from textual.reactive import reactive
from textual.binding import Binding


class PlanScreen(Screen):
    """Task planning and execution screen."""
    
    CSS = """
    PlanScreen {
        padding: 1;
    }
    
    #plan-header {
        height: auto;
        margin-bottom: 1;
    }
    
    #plan-tree {
        height: 1fr;
        border: solid $primary;
        padding: 1;
    }
    
    #plan-actions {
        height: auto;
        margin-top: 1;
    }
    
    #progress-area {
        height: auto;
        margin: 1 0;
    }
    
    .step-complete {
        text-style: strike;
        color: $text-muted;
    }
    
    .step-current {
        text-style: bold;
        background: $primary-darken-2;
    }
    
    .step-pending {
        color: $text;
    }
    """
    
    BINDINGS = [
        Binding("n", "new_step", "New Step", show=True),
        Binding("s", "spawn_agent", "Spawn Agent", show=True),
        Binding("space", "toggle_step", "Toggle", show=True),
    ]
    
    plan_title: reactive[str] = reactive("Untitled Plan")
    steps: reactive[list] = reactive([])
    current_step: reactive[int] = reactive(0)
    
    def compose(self):
        """Compose the plan screen."""
        with Vertical():
            # Header
            with Horizontal(id="plan-header"):
                yield Input(
                    value=self.plan_title,
                    placeholder="Plan title...",
                    id="plan-title-input",
                )
                yield Button("💾 Save", id="btn-save", variant="primary")
                yield Button("📂 Load", id="btn-load", variant="default")
            
            # Progress
            with Vertical(id="progress-area"):
                yield Label("Progress:")
                yield ProgressBar(total=100, id="plan-progress")
                yield Label("0/0 steps completed", id="progress-label")
            
            # Plan tree
            yield Label("Steps:")
            yield Tree("Plan", id="plan-tree")
            
            # Actions
            with Horizontal(id="plan-actions"):
                yield Button("➕ Add Step", id="btn-add-step", variant="primary")
                yield Button("🤖 Spawn Agent", id="btn-spawn", variant="success")
                yield Button("▶️  Run Step", id="btn-run", variant="default")
                yield Button("🗑️  Clear", id="btn-clear", variant="error")
    
    def on_mount(self):
        """Set up the plan screen."""
        tree = self.query_one("#plan-tree", Tree)
        
        # Mock plan data
        self.steps = [
            {"id": 1, "title": "Analyze codebase", "done": True, "agent": None},
            {"id": 2, "title": "Identify security issues", "done": True, "agent": None},
            {"id": 3, "title": "Fix SQL injection", "done": False, "agent": "agent-1"},
            {"id": 4, "title": "Add input validation", "done": False, "agent": None},
            {"id": 5, "title": "Write tests", "done": False, "agent": None},
        ]
        
        self.update_plan_display()
    
    def update_plan_display(self):
        """Update the plan tree display."""
        tree = self.query_one("#plan-tree", Tree)
        tree.clear()
        
        for i, step in enumerate(self.steps, 1):
            status = "✅" if step["done"] else "⬜"
            if i == self.current_step + 1 and not step["done"]:
                status = "▶️"
            
            label = f"{status} {i}. {step['title']}"
            if step["agent"]:
                label += f" (🤖 {step['agent']})"
            
            tree.root.add(label)
        
        # Update progress
        completed = sum(1 for s in self.steps if s["done"])
        total = len(self.steps)
        progress = (completed / total * 100) if total > 0 else 0
        
        self.query_one("#plan-progress", ProgressBar).update(progress=progress)
        self.query_one("#progress-label", Label).update(
            f"{completed}/{total} steps completed ({int(progress)}%)"
        )
    
    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle button presses."""
        if event.button.id == "btn-add-step":
            self.action_new_step()
        elif event.button.id == "btn-spawn":
            self.action_spawn_agent()
        elif event.button.id == "btn-run":
            self.app.notify("Running current step...", severity="information")
        elif event.button.id == "btn-clear":
            self.steps = []
            self.update_plan_display()
    
    def action_new_step(self) -> None:
        """Add a new step to the plan."""
        self.app.notify("New step dialog", severity="information")
    
    def action_spawn_agent(self) -> None:
        """Spawn an agent for current step."""
        self.app.notify("Spawn agent dialog", severity="information")
    
    def action_toggle_step(self) -> None:
        """Toggle completion of current step."""
        if 0 <= self.current_step < len(self.steps):
            self.steps[self.current_step]["done"] = not self.steps[self.current_step]["done"]
            self.update_plan_display()
