"""
obscura.tui.widgets -- TUI widget components.

All interactive widgets used by the Obscura TUI application:
message display, input, sidebar, status bar, diff view, plan view, etc.
"""

from obscura.tui.widgets.message_bubble import MessageBubble
from obscura.tui.widgets.message_list import MessageList
from obscura.tui.widgets.input_area import PromptInput
from obscura.tui.widgets.status_bar import StatusBar
from obscura.tui.widgets.sidebar import Sidebar
from obscura.tui.widgets.thinking_block import ThinkingBlock
from obscura.tui.widgets.tool_status import ToolStatus
from obscura.tui.widgets.diff_view import DiffView
from obscura.tui.widgets.plan_view import PlanView
from obscura.tui.widgets.file_tree import FileTree

__all__ = [
    "MessageBubble",
    "MessageList",
    "PromptInput",
    "StatusBar",
    "Sidebar",
    "ThinkingBlock",
    "ToolStatus",
    "DiffView",
    "PlanView",
    "FileTree",
]
