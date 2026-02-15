"""
sdk.tui.widgets.file_tree -- Modified files tree in sidebar.

Displays a tree of files that have been modified in Code mode,
with status icons indicating accepted/rejected/pending state.
"""

from __future__ import annotations

from pathlib import Path

from typing import override

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Static

from sdk.tui.modes import FileChange


class FileTree(Widget):
    """Displays a tree of modified files with status indicators.

    Status icons:
    - [ok]  = accepted
    - [xx]  = rejected
    - [..] = pending
    """

    DEFAULT_CSS = """
    FileTree {
        height: auto;
        padding: 0 1;
    }
    """

    # -- Messages -----------------------------------------------------------

    class FileSelected(Message):
        """Emitted when a file is clicked in the tree."""

        def __init__(self, path: Path) -> None:
            super().__init__()
            self.path = path

    # -- Init ---------------------------------------------------------------

    def __init__(
        self,
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes)
        self._changes: list[FileChange] = []
        self._container: Vertical | None = None

    # -- Compose ------------------------------------------------------------

    @override
    def compose(self) -> ComposeResult:
        yield Vertical(classes="file-tree")

    def on_mount(self) -> None:
        try:
            self._container = self.query_one(Vertical)
        except Exception:
            pass

    # -- Public API ---------------------------------------------------------

    def update_files(self, changes: list[FileChange]) -> None:
        """Update the displayed file list.

        Args:
            changes: List of FileChange objects to display.
        """
        self._changes = changes
        self._render_tree()

    def _render_tree(self) -> None:
        """Re-render the file tree."""
        if self._container is None:
            return

        # Remove existing entries
        for child in list(self._container.children):
            child.remove()

        if not self._changes:
            self._container.mount(Static("  (no changes)", classes="file-entry"))
            return

        # Group by directory for a tree-like display
        dirs: dict[str, list[FileChange]] = {}
        for change in self._changes:
            parent = str(change.path.parent)
            if parent == ".":
                parent = ""
            if parent not in dirs:
                dirs[parent] = []
            dirs[parent].append(change)

        for dir_path, files in sorted(dirs.items()):
            if dir_path:
                self._container.mount(
                    Static(f"  {dir_path}/", classes="file-entry dir")
                )

            for change in files:
                icon = self._status_icon(change.status)
                indent = "    " if dir_path else "  "
                fname = change.path.name
                css_class = f"file-entry {change.status}"

                self._container.mount(
                    Static(
                        f"{indent}{icon} {fname}",
                        classes=css_class,
                    )
                )

    @staticmethod
    def _status_icon(status: str) -> str:
        """Return a status icon for the given status."""
        icons = {
            "accepted": "[ok]",
            "rejected": "[xx]",
            "pending": "[..]",
        }
        return icons.get(status, "[??]")

    @property
    def file_count(self) -> int:
        """Number of files in the tree."""
        return len(self._changes)

    @property
    def pending_count(self) -> int:
        """Number of pending files."""
        return sum(1 for c in self._changes if c.status == "pending")
