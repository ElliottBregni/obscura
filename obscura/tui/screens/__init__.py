"""TUI Screens package."""

from obscura.tui.screens.dashboard import DashboardScreen
from obscura.tui.screens.chat import ChatScreen
from obscura.tui.screens.plan import PlanScreen
from obscura.tui.screens.code import CodeScreen
from obscura.tui.screens.diff import DiffScreen
from obscura.tui.screens.new_agent import NewAgentScreen

__all__ = [
    "DashboardScreen",
    "ChatScreen", 
    "PlanScreen",
    "CodeScreen",
    "DiffScreen",
    "NewAgentScreen",
]
