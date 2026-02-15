"""
sdk.tui.diff_engine -- Diff computation, hunk parsing, and patch application.

Uses Python's built-in ``difflib`` to compute unified diffs, then parses
them into structured hunks that can be selectively accepted or rejected.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, cast


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class DiffLine:
    """A single line in a diff hunk."""

    tag: Literal["+", "-", " "]
    content: str
    old_lineno: int | None = None
    new_lineno: int | None = None


@dataclass
class DiffHunk:
    """A contiguous group of changed lines in a diff."""

    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: list[DiffLine] = field(default_factory=lambda: cast(list[DiffLine], []))
    status: str = "pending"  # "pending" | "accepted" | "rejected"
    header: str = ""         # The @@ line

    def accept(self) -> None:
        self.status = "accepted"

    def reject(self) -> None:
        self.status = "rejected"


@dataclass
class FileChange:
    """A complete file change with computed hunks."""

    path: Path
    original: str
    modified: str
    hunks: list[DiffHunk] = field(default_factory=lambda: cast(list[DiffHunk], []))
    status: str = "pending"  # "pending" | "accepted" | "rejected"

    def accept_all(self) -> None:
        """Accept all hunks and mark file as accepted."""
        for h in self.hunks:
            h.accept()
        self.status = "accepted"

    def reject_all(self) -> None:
        """Reject all hunks and mark file as rejected."""
        for h in self.hunks:
            h.reject()
        self.status = "rejected"

    @property
    def all_decided(self) -> bool:
        return all(h.status != "pending" for h in self.hunks)

    @property
    def accepted_count(self) -> int:
        return sum(1 for h in self.hunks if h.status == "accepted")

    @property
    def rejected_count(self) -> int:
        return sum(1 for h in self.hunks if h.status == "rejected")

    @property
    def pending_count(self) -> int:
        return sum(1 for h in self.hunks if h.status == "pending")


# ---------------------------------------------------------------------------
# DiffEngine
# ---------------------------------------------------------------------------

class DiffEngine:
    """Compute diffs, parse hunks, format output, and apply patches."""

    def __init__(self, context_lines: int = 3) -> None:
        self._context = context_lines

    # -- Computation --------------------------------------------------------

    def compute(self, original: str, modified: str) -> list[DiffHunk]:
        """Compute diff hunks between original and modified text.

        Args:
            original: The original file content.
            modified: The modified file content.

        Returns:
            A list of DiffHunk objects representing the changes.
        """
        old_lines = original.splitlines(keepends=True)
        new_lines = modified.splitlines(keepends=True)

        # Ensure trailing newline for clean diffs
        if old_lines and not old_lines[-1].endswith("\n"):
            old_lines[-1] += "\n"
        if new_lines and not new_lines[-1].endswith("\n"):
            new_lines[-1] += "\n"

        diff = list(difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile="original",
            tofile="modified",
            n=self._context,
        ))

        return self._parse_unified(diff)

    def compute_change(self, path: Path, original: str, modified: str) -> FileChange:
        """Compute a complete FileChange with hunks.

        Args:
            path: The file path.
            original: The original content.
            modified: The modified content.

        Returns:
            A FileChange with parsed hunks.
        """
        hunks = self.compute(original, modified)
        return FileChange(
            path=path,
            original=original,
            modified=modified,
            hunks=hunks,
        )

    # -- Parsing ------------------------------------------------------------

    def _parse_unified(self, diff_lines: list[str]) -> list[DiffHunk]:
        """Parse unified diff output into DiffHunk objects."""
        hunks: list[DiffHunk] = []
        current_hunk: DiffHunk | None = None
        old_lineno = 0
        new_lineno = 0

        for line in diff_lines:
            # Skip file headers
            if line.startswith("---") or line.startswith("+++"):
                continue

            # Hunk header
            if line.startswith("@@"):
                # Parse @@ -old_start,old_count +new_start,new_count @@
                header = line.strip()
                parts = header.split()
                old_range = parts[1]  # -old_start,old_count
                new_range = parts[2]  # +new_start,new_count

                old_start, old_count = self._parse_range(old_range[1:])
                new_start, new_count = self._parse_range(new_range[1:])

                current_hunk = DiffHunk(
                    old_start=old_start,
                    old_count=old_count,
                    new_start=new_start,
                    new_count=new_count,
                    header=header,
                )
                hunks.append(current_hunk)
                old_lineno = old_start
                new_lineno = new_start
                continue

            if current_hunk is None:
                continue

            # Diff lines
            content = line[1:] if len(line) > 1 else ""
            # Strip trailing newline from content for display
            content = content.rstrip("\n")

            if line.startswith("+"):
                current_hunk.lines.append(DiffLine(
                    tag="+",
                    content=content,
                    old_lineno=None,
                    new_lineno=new_lineno,
                ))
                new_lineno += 1
            elif line.startswith("-"):
                current_hunk.lines.append(DiffLine(
                    tag="-",
                    content=content,
                    old_lineno=old_lineno,
                    new_lineno=None,
                ))
                old_lineno += 1
            elif line.startswith(" "):
                current_hunk.lines.append(DiffLine(
                    tag=" ",
                    content=content,
                    old_lineno=old_lineno,
                    new_lineno=new_lineno,
                ))
                old_lineno += 1
                new_lineno += 1

        return hunks

    @staticmethod
    def _parse_range(s: str) -> tuple[int, int]:
        """Parse '123,45' or '123' into (start, count)."""
        if "," in s:
            parts = s.split(",")
            return int(parts[0]), int(parts[1])
        return int(s), 1

    # -- Application --------------------------------------------------------

    def apply_hunks(
        self,
        original: str,
        hunks: list[DiffHunk],
    ) -> str:
        """Apply accepted hunks to the original text.

        Only hunks with status 'accepted' are applied. Rejected and
        pending hunks are skipped (original content preserved).

        Args:
            original: The original file content.
            hunks: The list of hunks to consider.

        Returns:
            The patched content with accepted hunks applied.
        """
        if not hunks:
            return original

        old_lines = original.splitlines(keepends=True)
        # Ensure trailing newlines
        if old_lines and not old_lines[-1].endswith("\n"):
            old_lines[-1] += "\n"

        result: list[str] = []
        old_idx = 0  # 0-based index into old_lines

        # Sort hunks by old_start to apply in order
        sorted_hunks = sorted(hunks, key=lambda h: h.old_start)

        for hunk in sorted_hunks:
            # Copy unchanged lines before this hunk (old_start is 1-based)
            hunk_start_0 = hunk.old_start - 1
            while old_idx < hunk_start_0 and old_idx < len(old_lines):
                result.append(old_lines[old_idx])
                old_idx += 1

            # Apply hunk: add "+" lines, skip "-" lines
            for dline in hunk.lines:
                if dline.tag == "+":
                    result.append(dline.content + "\n")
                elif dline.tag == "-":
                    old_idx += 1
                elif dline.tag == " ":
                    if old_idx < len(old_lines):
                        result.append(old_lines[old_idx])
                    old_idx += 1

        # Copy remaining lines after last hunk
        while old_idx < len(old_lines):
            result.append(old_lines[old_idx])
            old_idx += 1

        return "".join(result)

    # -- Formatting ---------------------------------------------------------

    def format_unified(self, change: FileChange) -> str:
        """Format a FileChange as a unified diff string.

        Args:
            change: The FileChange to format.

        Returns:
            A unified diff string with +/- line markers.
        """
        old_lines = change.original.splitlines(keepends=True)
        new_lines = change.modified.splitlines(keepends=True)

        diff = difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=str(change.path),
            tofile=str(change.path),
            n=self._context,
        )
        return "".join(diff)

    def format_side_by_side(
        self,
        change: FileChange,
        width: int = 80,
    ) -> str:
        """Format a FileChange as a side-by-side diff.

        Args:
            change: The FileChange to format.
            width: Total width of the output.

        Returns:
            A side-by-side diff string.
        """
        half = (width - 3) // 2  # 3 for " | " separator
        gutter_w = 5
        content_w = half - gutter_w - 1

        lines: list[str] = []

        # Header
        header = f"--- {change.path}  |  +++ {change.path}"
        lines.append(header)
        lines.append("-" * width)

        for hunk in change.hunks:
            # Hunk header
            lines.append(f"{'=' * width}")

            # Separate additions and deletions
            old_lines: list[tuple[int | None, str]] = []
            new_lines: list[tuple[int | None, str]] = []

            for dline in hunk.lines:
                if dline.tag == "-":
                    old_lines.append((dline.old_lineno, dline.content))
                elif dline.tag == "+":
                    new_lines.append((dline.new_lineno, dline.content))
                elif dline.tag == " ":
                    # Flush any pending changes
                    max_len = max(len(old_lines), len(new_lines))
                    while len(old_lines) < max_len:
                        old_lines.append((None, ""))
                    while len(new_lines) < max_len:
                        new_lines.append((None, ""))

                    for (oln, oc), (nln, nc) in zip(old_lines, new_lines):
                        o_gutter = f"{oln:>{gutter_w}}" if oln else " " * gutter_w
                        n_gutter = f"{nln:>{gutter_w}}" if nln else " " * gutter_w
                        o_text = oc[:content_w].ljust(content_w)
                        n_text = nc[:content_w].ljust(content_w)
                        lines.append(f"{o_gutter} {o_text} | {n_gutter} {n_text}")

                    old_lines.clear()
                    new_lines.clear()

                    # Context line
                    oln = dline.old_lineno
                    nln = dline.new_lineno
                    o_gutter = f"{oln:>{gutter_w}}" if oln else " " * gutter_w
                    n_gutter = f"{nln:>{gutter_w}}" if nln else " " * gutter_w
                    text = dline.content[:content_w].ljust(content_w)
                    lines.append(f"{o_gutter} {text} | {n_gutter} {text}")

            # Flush remaining
            max_len = max(len(old_lines), len(new_lines))
            while len(old_lines) < max_len:
                old_lines.append((None, ""))
            while len(new_lines) < max_len:
                new_lines.append((None, ""))

            for (oln, oc), (nln, nc) in zip(old_lines, new_lines):
                o_gutter = f"{oln:>{gutter_w}}" if oln else " " * gutter_w
                n_gutter = f"{nln:>{gutter_w}}" if nln else " " * gutter_w
                o_text = oc[:content_w].ljust(content_w)
                n_text = nc[:content_w].ljust(content_w)
                lines.append(f"{o_gutter} {o_text} | {n_gutter} {n_text}")

        return "\n".join(lines)
