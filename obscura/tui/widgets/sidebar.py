"""
obscura.tui.widgets.sidebar -- Sidebar with mode selector and info panels.

Displays:
- Mode selector (radio-style list)
- Backend + model info
- Session info (ID, turn count, duration)
- File tree of changed files (Code mode)
- File status list (Diff mode)
- Memory namespace browser
"""

from __future__ import annotations

from typing import override

from textual import events
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static

from obscura.tui.modes import FileChange, TUIMode
from obscura.tui.widgets.file_tree import FileTree


class Sidebar(Widget):
    """Left sidebar with mode selector and contextual info panels.

    Visibility is toggled with Ctrl+B.
    Content adapts based on the current mode:
    - Ask/Plan: session info
    - Code: file tree of changed files
    - Diff: file status list with accept/reject icons
    """

    DEFAULT_CSS = """
    Sidebar {
        dock: left;
        width: 28;
        padding: 1;
    }
    """

    mode: reactive[TUIMode] = reactive(TUIMode.ASK)
    backend: reactive[str] = reactive("copilot")
    model_name: reactive[str] = reactive("")
    session_id: reactive[str] = reactive("")
    turn_count: reactive[int] = reactive(0)
    sidebar_visible: reactive[bool] = reactive(True)

    # -- Messages -----------------------------------------------------------

    class ModeSelected(Message):
        """Emitted when the user selects a mode from the sidebar."""

        def __init__(self, mode: TUIMode) -> None:
            super().__init__()
            self.mode = mode

    # -- Init ---------------------------------------------------------------

    def __init__(
        self,
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id or "sidebar", classes=classes)
        self._file_tree: FileTree | None = None
        self._mode_widgets: dict[TUIMode, Static] = {}
        self._backend_widget: Static | None = None
        self._session_widget: Static | None = None
        self._turns_widget: Static | None = None
        self._info_container: Vertical | None = None

    # -- Compose ------------------------------------------------------------

    @override
    def compose(self) -> ComposeResult:
        yield Static("OBSCURA", classes="sidebar-title")

        # Mode selector
        yield Static("MODE", classes="sidebar-section")
        for m in TUIMode:
            indicator = "*" if m == self.mode else " "
            yield Static(
                f"  {indicator} {m.value.capitalize()}",
                classes="mode-item",
                id=f"mode-{m.value}",
            )

        # Backend info
        yield Static("BACKEND", classes="sidebar-section")
        yield Static(
            f"  {self.backend}",
            classes="sidebar-value",
            id="sb-backend-info",
        )

        # Session info
        yield Static("SESSION", classes="sidebar-section")
        yield Static(
            f"  {self.session_id[:8] if self.session_id else 'none'}",
            classes="sidebar-value",
            id="sb-session-info",
        )
        yield Static(
            f"  Turns: {self.turn_count}",
            classes="sidebar-value",
            id="sb-turns-info",
        )

        # Context-dependent section
        with Vertical(id="sidebar-context"):
            yield Static("FILES", classes="sidebar-section", id="files-header")
            yield FileTree(id="sidebar-file-tree")

    def on_mount(self) -> None:
        """Cache widget references."""
        try:
            for m in TUIMode:
                w = self.query_one(f"#mode-{m.value}", Static)
                self._mode_widgets[m] = w
            self._backend_widget = self.query_one("#sb-backend-info", Static)
            self._session_widget = self.query_one("#sb-session-info", Static)
            self._turns_widget = self.query_one("#sb-turns-info", Static)
            self._file_tree = self.query_one("#sidebar-file-tree", FileTree)
        except Exception:
            pass
        self._update_mode_display()
        self._update_context_display()

    # -- Click handling for mode selection ----------------------------------

    async def on_click(self, event: events.Click) -> None:
        """Handle clicks on mode items."""
        # Check if a mode item was clicked
        for m in TUIMode:
            widget = self._mode_widgets.get(m)
            if widget and widget is event.widget:
                self.post_message(self.ModeSelected(m))
                return

    # -- Watchers -----------------------------------------------------------

    def watch_mode(self, value: TUIMode) -> None:
        self._update_mode_display()
        self._update_context_display()

    def watch_backend(self, value: str) -> None:
        if self._backend_widget:
            model_str = f"/{self.model_name}" if self.model_name else ""
            self._backend_widget.update(f"  {value}{model_str}")

    def watch_model_name(self, value: str) -> None:
        if self._backend_widget:
            model_str = f"/{value}" if value else ""
            self._backend_widget.update(f"  {self.backend}{model_str}")

    def watch_session_id(self, value: str) -> None:
        if self._session_widget:
            sid = value[:8] if value else "none"
            self._session_widget.update(f"  {sid}")

    def watch_turn_count(self, value: int) -> None:
        if self._turns_widget:
            self._turns_widget.update(f"  Turns: {value}")

    def watch_sidebar_visible(self, value: bool) -> None:
        if value:
            self.remove_class("hidden")
        else:
            self.add_class("hidden")
        self.display = value

    # -- Internal -----------------------------------------------------------

    def _update_mode_display(self) -> None:
        """Update mode indicator icons."""
        for m, widget in self._mode_widgets.items():
            indicator = "*" if m == self.mode else " "
            widget.update(f"  {indicator} {m.value.capitalize()}")
            if m == self.mode:
                widget.add_class("active")
            else:
                widget.remove_class("active")

    def _update_context_display(self) -> None:
        """Show/hide the file tree based on current mode."""
        try:
            files_header = self.query_one("#files-header", Static)
            file_tree = self.query_one("#sidebar-file-tree", FileTree)
        except Exception:
            return

        if self.mode in (TUIMode.CODE, TUIMode.DIFF):
            files_header.display = True
            file_tree.display = True
        else:
            files_header.display = False
            file_tree.display = False

    # -- Public API ---------------------------------------------------------

    def update_mode(self, mode: TUIMode) -> None:
        """Set the current mode."""
        self.mode = mode

    def update_backend(self, backend: str, model: str | None = None) -> None:
        """Update backend/model display."""
        self.backend = backend
        if model is not None:
            self.model_name = model

    def update_session(self, session_id: str, turn_count: int = 0) -> None:
        """Update session info."""
        self.session_id = session_id
        self.turn_count = turn_count

    def update_files(self, changes: list[FileChange]) -> None:
        """Update the file tree with current changes."""
        if self._file_tree:
            self._file_tree.update_files(changes)

    def toggle_visibility(self) -> None:
        """Toggle sidebar visibility."""
        self.sidebar_visible = not self.sidebar_visible
