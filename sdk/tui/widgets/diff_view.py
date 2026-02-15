"""
sdk.tui.widgets.diff_view -- Unified and side-by-side diff display.

Displays file changes as colored diffs with hunk-level navigation
and per-hunk accept/reject controls.

Keybindings (when focused):
- j/k: Navigate between hunks
- a: Accept current hunk
- r: Reject current hunk
- A: Accept all hunks in current file
- R: Reject all hunks in current file
- d: Toggle unified/side-by-side view
"""

from __future__ import annotations

from typing import override

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static

from sdk.tui.diff_engine import DiffEngine, DiffHunk, FileChange


class DiffView(Widget, can_focus=True):
    """Interactive diff viewer with hunk navigation and accept/reject.

    Supports unified and side-by-side display modes with per-hunk
    status control. Hunks can be navigated with j/k keys and
    accepted/rejected individually or as a group.
    """

    DEFAULT_CSS = """
    DiffView {
        height: auto;
        padding: 0;
        overflow-y: auto;
    }
    """

    BINDINGS = [
        Binding("j", "next_hunk", "Next hunk", show=False),
        Binding("k", "prev_hunk", "Previous hunk", show=False),
        Binding("a", "accept_hunk", "Accept hunk", show=False),
        Binding("r", "reject_hunk", "Reject hunk", show=False),
        Binding("A", "accept_all", "Accept all", show=False),
        Binding("R", "reject_all", "Reject all", show=False),
        Binding("d", "toggle_view", "Toggle view", show=False),
    ]

    current_hunk_idx: reactive[int] = reactive(0)
    side_by_side: reactive[bool] = reactive(False)

    # -- Messages -----------------------------------------------------------

    class HunkAccepted(Message):
        """Emitted when a hunk is accepted."""

        def __init__(self, file_path: str, hunk_idx: int) -> None:
            super().__init__()
            self.file_path = file_path
            self.hunk_idx = hunk_idx

    class HunkRejected(Message):
        """Emitted when a hunk is rejected."""

        def __init__(self, file_path: str, hunk_idx: int) -> None:
            super().__init__()
            self.file_path = file_path
            self.hunk_idx = hunk_idx

    class AllAccepted(Message):
        """Emitted when all hunks in a file are accepted."""

        def __init__(self, file_path: str) -> None:
            super().__init__()
            self.file_path = file_path

    class AllRejected(Message):
        """Emitted when all hunks in a file are rejected."""

        def __init__(self, file_path: str) -> None:
            super().__init__()
            self.file_path = file_path

    # -- Init ---------------------------------------------------------------

    def __init__(
        self,
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes)
        self._change: FileChange | None = None
        self._diff_engine = DiffEngine()
        self._content_container: Vertical | None = None
        self._hunk_widgets: list[Static] = []

    # -- Compose ------------------------------------------------------------

    @override
    def compose(self) -> ComposeResult:
        with VerticalScroll(classes="diff-view"):
            with Vertical(id="diff-content"):
                yield Static(
                    "No diff to display. Make changes in Code mode first.",
                    classes="diff-empty",
                )

    def on_mount(self) -> None:
        try:
            self._content_container = self.query_one("#diff-content", Vertical)
        except Exception:
            pass

    # -- Public API ---------------------------------------------------------

    def set_change(self, change: FileChange) -> None:
        """Display a FileChange as a diff.

        Args:
            change: The FileChange to display.
        """
        self._change = change
        self.current_hunk_idx = 0
        self._render_diff()

    def set_changes(self, changes: list[FileChange]) -> None:
        """Display multiple file changes.

        Shows each file's diff sequentially.
        """
        if not changes:
            self._change = None
            self._render_empty()
            return

        # For now, show the first change; navigation between files
        # can be added via the sidebar file tree
        self._change = changes[0]
        self.current_hunk_idx = 0
        self._render_diff()

    def clear(self) -> None:
        """Clear the diff display."""
        self._change = None
        self._render_empty()

    # -- Rendering ----------------------------------------------------------

    def _render_empty(self) -> None:
        """Show empty state."""
        if self._content_container is None:
            return
        for child in list(self._content_container.children):
            child.remove()
        self._content_container.mount(
            Static(
                "No diff to display.",
                classes="diff-empty",
            )
        )
        self._hunk_widgets.clear()

    def _render_diff(self) -> None:
        """Render the current diff."""
        if self._content_container is None or self._change is None:
            return

        # Clear existing content
        for child in list(self._content_container.children):
            child.remove()
        self._hunk_widgets.clear()

        change = self._change

        # File header
        self._content_container.mount(
            Static(
                f"--- {change.path} -> +++ {change.path}",
                classes="diff-header",
            )
        )

        if not change.hunks:
            self._content_container.mount(Static("(no changes)", classes="diff-empty"))
            return

        # Render each hunk
        for idx, hunk in enumerate(change.hunks):
            # Hunk header
            hunk_header = hunk.header or (
                f"@@ -{hunk.old_start},{hunk.old_count} "
                f"+{hunk.new_start},{hunk.new_count} @@"
            )

            # Status indicator
            status_icon = {
                "pending": "[..]",
                "accepted": "[OK]",
                "rejected": "[NO]",
            }.get(hunk.status, "")

            selected = " <<" if idx == self.current_hunk_idx else ""

            self._content_container.mount(
                Static(
                    f"{hunk_header}  {status_icon}{selected}",
                    classes="diff-hunk-header",
                    id=f"hunk-header-{idx}",
                )
            )

            # Hunk lines
            if self.side_by_side:
                self._render_hunk_side_by_side(hunk, idx)
            else:
                self._render_hunk_unified(hunk, idx)

    def _render_hunk_unified(self, hunk: DiffHunk, idx: int) -> None:
        """Render a hunk in unified diff format."""
        if self._content_container is None:
            return

        for line in hunk.lines:
            if line.tag == "+":
                gutter = f"{line.new_lineno or '':>5} " if line.new_lineno else "      "
                css_class = "diff-line-add"
                prefix = "+"
            elif line.tag == "-":
                gutter = f"{line.old_lineno or '':>5} " if line.old_lineno else "      "
                css_class = "diff-line-del"
                prefix = "-"
            else:
                gutter = f"{line.old_lineno or '':>5} " if line.old_lineno else "      "
                css_class = "diff-line-ctx"
                prefix = " "

            # Add selected class if this is the current hunk
            extra = " hunk-selected" if idx == self.current_hunk_idx else ""

            self._content_container.mount(
                Static(
                    f"{gutter}{prefix}{line.content}",
                    classes=f"{css_class}{extra}",
                )
            )

    def _render_hunk_side_by_side(self, hunk: DiffHunk, idx: int) -> None:
        """Render a hunk in side-by-side format."""
        if self._content_container is None:
            return

        # Collect old/new lines
        old_lines: list[tuple[int | None, str]] = []
        new_lines: list[tuple[int | None, str]] = []
        pairs: list[tuple[str, str]] = []

        for line in hunk.lines:
            if line.tag == "-":
                old_lines.append((line.old_lineno, line.content))
            elif line.tag == "+":
                new_lines.append((line.new_lineno, line.content))
            else:
                # Flush pending changes
                max_len = max(len(old_lines), len(new_lines))
                while len(old_lines) < max_len:
                    old_lines.append((None, ""))
                while len(new_lines) < max_len:
                    new_lines.append((None, ""))

                for (oln, oc), (nln, nc) in zip(old_lines, new_lines):
                    o_g = f"{oln:>5}" if oln else "     "
                    n_g = f"{nln:>5}" if nln else "     "
                    pairs.append((f"{o_g} -{oc}", f"{n_g} +{nc}"))

                old_lines.clear()
                new_lines.clear()

                # Context line
                g = f"{line.old_lineno:>5}" if line.old_lineno else "     "
                pairs.append((f"{g}  {line.content}", f"{g}  {line.content}"))

        # Flush remaining
        max_len = max(len(old_lines), len(new_lines), 0)
        while len(old_lines) < max_len:
            old_lines.append((None, ""))
        while len(new_lines) < max_len:
            new_lines.append((None, ""))

        for (oln, oc), (nln, nc) in zip(old_lines, new_lines):
            o_g = f"{oln:>5}" if oln else "     "
            n_g = f"{nln:>5}" if nln else "     "
            pairs.append((f"{o_g} -{oc}", f"{n_g} +{nc}"))

        # Render pairs
        for left, right in pairs:
            half_width = 40
            left_padded = left[:half_width].ljust(half_width)
            right_padded = right[:half_width].ljust(half_width)
            extra = " hunk-selected" if idx == self.current_hunk_idx else ""

            self._content_container.mount(
                Static(
                    f"{left_padded} | {right_padded}",
                    classes=f"diff-line-ctx{extra}",
                )
            )

    # -- Hunk navigation ----------------------------------------------------

    def action_next_hunk(self) -> None:
        """Move to the next hunk."""
        if self._change and self.current_hunk_idx < len(self._change.hunks) - 1:
            self.current_hunk_idx += 1
            self._render_diff()

    def action_prev_hunk(self) -> None:
        """Move to the previous hunk."""
        if self._change and self.current_hunk_idx > 0:
            self.current_hunk_idx -= 1
            self._render_diff()

    def action_accept_hunk(self) -> None:
        """Accept the current hunk."""
        if not self._change or not self._change.hunks:
            return
        hunk = self._change.hunks[self.current_hunk_idx]
        hunk.accept()
        self.post_message(
            self.HunkAccepted(str(self._change.path), self.current_hunk_idx)
        )
        self._check_file_status()
        self._render_diff()

    def action_reject_hunk(self) -> None:
        """Reject the current hunk."""
        if not self._change or not self._change.hunks:
            return
        hunk = self._change.hunks[self.current_hunk_idx]
        hunk.reject()
        self.post_message(
            self.HunkRejected(str(self._change.path), self.current_hunk_idx)
        )
        self._check_file_status()
        self._render_diff()

    def action_accept_all(self) -> None:
        """Accept all hunks in the current file."""
        if not self._change:
            return
        self._change.accept_all()
        self.post_message(self.AllAccepted(str(self._change.path)))
        self._render_diff()

    def action_reject_all(self) -> None:
        """Reject all hunks in the current file."""
        if not self._change:
            return
        self._change.reject_all()
        self.post_message(self.AllRejected(str(self._change.path)))
        self._render_diff()

    def action_toggle_view(self) -> None:
        """Toggle between unified and side-by-side view."""
        self.side_by_side = not self.side_by_side
        self._render_diff()

    def _check_file_status(self) -> None:
        """Update file-level status based on hunk statuses."""
        if not self._change:
            return
        if self._change.all_decided:
            if self._change.accepted_count > 0 and self._change.rejected_count == 0:
                self._change.status = "accepted"
            elif self._change.rejected_count > 0 and self._change.accepted_count == 0:
                self._change.status = "rejected"
            # Mixed decisions keep status as "pending" until user resolves

    # -- Watchers -----------------------------------------------------------

    def watch_current_hunk_idx(self, value: int) -> None:
        """Re-render on hunk index change."""
        pass  # Render happens in the action methods

    def watch_side_by_side(self, value: bool) -> None:
        """Re-render on view mode change."""
        pass  # Render happens in action_toggle_view

    # -- Properties ---------------------------------------------------------

    @property
    def change(self) -> FileChange | None:
        return self._change

    @property
    def hunk_count(self) -> int:
        if self._change:
            return len(self._change.hunks)
        return 0

    @property
    def current_hunk(self) -> DiffHunk | None:
        if self._change and self._change.hunks:
            return self._change.hunks[self.current_hunk_idx]
        return None
