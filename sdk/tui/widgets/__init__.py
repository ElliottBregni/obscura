"""
sdk.tui.widgets -- TUI widget components.

All interactive widgets used by the Obscura TUI application:
message display, input, sidebar, status bar, diff view, plan view, etc.
"""

from sdk.tui.widgets.message_bubble import MessageBubble
from sdk.tui.widgets.message_list import MessageList
from sdk.tui.widgets.input_area import PromptInput
from sdk.tui.widgets.status_bar import StatusBar
from sdk.tui.widgets.sidebar import Sidebar
from sdk.tui.widgets.thinking_block import ThinkingBlock
from sdk.tui.widgets.tool_status import ToolStatus
from sdk.tui.widgets.diff_view import DiffView
from sdk.tui.widgets.plan_view import PlanView
from sdk.tui.widgets.file_tree import FileTree

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
