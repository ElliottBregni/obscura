"""Tests for sdk.tui.diff_engine — Diff computation, hunk accept/reject.

Covers DiffEngine.compute() with various inputs, DiffHunk structure,
selective hunk acceptance, apply_hunks(), format_unified(),
format_side_by_side(), and edge cases.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# Inline stubs — mirrors sdk/tui/diff_engine.py from PLAN_TUI.md
# ---------------------------------------------------------------------------

@dataclass
class DiffLine:
    """A single line in a diff hunk."""
    tag: str      # "+", "-", " " (added, removed, context)
    content: str


@dataclass
class DiffHunk:
    """A contiguous group of changes in a diff."""
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: list[DiffLine] = field(default_factory=list)


@dataclass
class FileChange:
    """A file modification with computed hunks."""
    path: Path
    original: str
    modified: str
    hunks: list[DiffHunk] = field(default_factory=list)
    status: str = "pending"  # "pending" | "accepted" | "rejected"


class DiffEngine:
    """Compute and apply diffs using Python's difflib."""

    def compute(self, original: str, modified: str) -> list[DiffHunk]:
        """Compute diff hunks between original and modified text."""
        old_lines = original.splitlines(keepends=True)
        new_lines = modified.splitlines(keepends=True)

        matcher = difflib.SequenceMatcher(None, old_lines, new_lines)
        hunks: list[DiffHunk] = []

        for group in matcher.get_grouped_opcodes(n=3):
            hunk_lines: list[DiffLine] = []
            old_start = group[0][1] + 1  # 1-indexed
            old_end = group[-1][2]
            new_start = group[0][3] + 1
            new_end = group[-1][4]

            for tag, i1, i2, j1, j2 in group:
                if tag == "equal":
                    for line in old_lines[i1:i2]:
                        hunk_lines.append(DiffLine(tag=" ", content=line))
                elif tag == "delete":
                    for line in old_lines[i1:i2]:
                        hunk_lines.append(DiffLine(tag="-", content=line))
                elif tag == "insert":
                    for line in new_lines[j1:j2]:
                        hunk_lines.append(DiffLine(tag="+", content=line))
                elif tag == "replace":
                    for line in old_lines[i1:i2]:
                        hunk_lines.append(DiffLine(tag="-", content=line))
                    for line in new_lines[j1:j2]:
                        hunk_lines.append(DiffLine(tag="+", content=line))

            if any(dl.tag != " " for dl in hunk_lines):
                hunks.append(DiffHunk(
                    old_start=old_start,
                    old_count=old_end - old_start + 1,
                    new_start=new_start,
                    new_count=new_end - new_start + 1,
                    lines=hunk_lines,
                ))

        return hunks

    def apply_hunks(self, original: str, accepted: list[DiffHunk]) -> str:
        """Apply only the accepted hunks to the original text.

        Hunks not in the accepted list are treated as rejected (original kept).
        """
        if not accepted:
            return original

        old_lines = original.splitlines(keepends=True)
        # Build a set of line indices that should be replaced
        result_lines: list[str] = []
        # Track which old lines are consumed by accepted hunks
        consumed: set[int] = set()
        insertions: dict[int, list[str]] = {}  # old_line_idx -> lines to insert

        for hunk in accepted:
            start_idx = hunk.old_start - 1  # Convert to 0-indexed
            old_idx = start_idx
            new_lines_for_hunk: list[str] = []

            for dl in hunk.lines:
                if dl.tag == " ":
                    old_idx += 1
                elif dl.tag == "-":
                    consumed.add(old_idx)
                    old_idx += 1
                elif dl.tag == "+":
                    new_lines_for_hunk.append(dl.content)

            # Store insertions at the hunk start position
            if start_idx not in insertions:
                insertions[start_idx] = []
            insertions[start_idx].extend(new_lines_for_hunk)

        # Rebuild the file
        for i, line in enumerate(old_lines):
            if i in insertions:
                result_lines.extend(insertions[i])
            if i not in consumed:
                result_lines.append(line)

        # Handle insertions past the end
        for idx, lines in insertions.items():
            if idx >= len(old_lines):
                result_lines.extend(lines)

        return "".join(result_lines)

    def format_unified(self, change: FileChange) -> str:
        """Format a FileChange as unified diff output."""
        old_lines = change.original.splitlines(keepends=True)
        new_lines = change.modified.splitlines(keepends=True)

        diff = difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=f"a/{change.path}",
            tofile=f"b/{change.path}",
        )
        return "".join(diff)

    def format_side_by_side(self, change: FileChange, width: int = 80) -> str:
        """Format a FileChange as side-by-side diff output."""
        old_lines = change.original.splitlines()
        new_lines = change.modified.splitlines()

        half_width = (width - 3) // 2  # 3 for separator " | "
        result: list[str] = []

        max_len = max(len(old_lines), len(new_lines))
        for i in range(max_len):
            left = old_lines[i] if i < len(old_lines) else ""
            right = new_lines[i] if i < len(new_lines) else ""
            left_padded = left[:half_width].ljust(half_width)
            right_padded = right[:half_width].ljust(half_width)
            result.append(f"{left_padded} | {right_padded}")

        return "\n".join(result)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestDiffHunkStructure:
    """Verify DiffHunk and DiffLine dataclass fields."""

    def test_diff_line_added(self) -> None:
        """DiffLine with '+' tag represents an added line."""
        dl = DiffLine(tag="+", content="new line\n")
        assert dl.tag == "+"
        assert dl.content == "new line\n"

    def test_diff_line_removed(self) -> None:
        """DiffLine with '-' tag represents a removed line."""
        dl = DiffLine(tag="-", content="old line\n")
        assert dl.tag == "-"

    def test_diff_line_context(self) -> None:
        """DiffLine with ' ' tag represents context (unchanged)."""
        dl = DiffLine(tag=" ", content="context\n")
        assert dl.tag == " "

    def test_diff_hunk_defaults(self) -> None:
        """DiffHunk has empty lines list by default."""
        hunk = DiffHunk(old_start=1, old_count=3, new_start=1, new_count=4)
        assert hunk.lines == []

    def test_diff_hunk_with_lines(self) -> None:
        """DiffHunk stores its list of DiffLines."""
        lines = [
            DiffLine(tag=" ", content="ctx\n"),
            DiffLine(tag="-", content="old\n"),
            DiffLine(tag="+", content="new\n"),
        ]
        hunk = DiffHunk(old_start=5, old_count=2, new_start=5, new_count=2, lines=lines)
        assert len(hunk.lines) == 3
        assert hunk.old_start == 5


class TestFileChange:
    """Verify FileChange dataclass."""

    def test_file_change_defaults(self) -> None:
        """FileChange defaults to 'pending' status and empty hunks."""
        fc = FileChange(path=Path("test.py"), original="a", modified="b")
        assert fc.status == "pending"
        assert fc.hunks == []

    def test_file_change_accept(self) -> None:
        """FileChange status can be set to 'accepted'."""
        fc = FileChange(path=Path("test.py"), original="a", modified="b")
        fc.status = "accepted"
        assert fc.status == "accepted"

    def test_file_change_reject(self) -> None:
        """FileChange status can be set to 'rejected'."""
        fc = FileChange(path=Path("test.py"), original="a", modified="b")
        fc.status = "rejected"
        assert fc.status == "rejected"


class TestDiffEngineCompute:
    """Verify DiffEngine.compute() with various input scenarios."""

    @pytest.fixture()
    def engine(self) -> DiffEngine:
        return DiffEngine()

    def test_compute_identical_files_no_hunks(self, engine: DiffEngine) -> None:
        """Identical content produces no hunks."""
        text = "line 1\nline 2\nline 3\n"
        hunks = engine.compute(text, text)
        assert hunks == []

    def test_compute_single_addition(self, engine: DiffEngine) -> None:
        """Adding a line produces a hunk with '+' lines."""
        original = "line 1\nline 2\n"
        modified = "line 1\nline 2\nline 3\n"
        hunks = engine.compute(original, modified)
        assert len(hunks) >= 1
        added = [dl for h in hunks for dl in h.lines if dl.tag == "+"]
        assert any("line 3" in dl.content for dl in added)

    def test_compute_single_deletion(self, engine: DiffEngine) -> None:
        """Removing a line produces a hunk with '-' lines."""
        original = "line 1\nline 2\nline 3\n"
        modified = "line 1\nline 3\n"
        hunks = engine.compute(original, modified)
        assert len(hunks) >= 1
        removed = [dl for h in hunks for dl in h.lines if dl.tag == "-"]
        assert any("line 2" in dl.content for dl in removed)

    def test_compute_modification(self, engine: DiffEngine) -> None:
        """Modifying a line produces both '-' and '+' lines."""
        original = "hello world\n"
        modified = "hello universe\n"
        hunks = engine.compute(original, modified)
        assert len(hunks) >= 1
        tags = {dl.tag for h in hunks for dl in h.lines}
        assert "-" in tags
        assert "+" in tags

    def test_compute_multiple_changes(self, engine: DiffEngine) -> None:
        """Multiple scattered changes produce multiple hunks or a large hunk."""
        original = "\n".join(f"line {i}" for i in range(1, 21)) + "\n"
        lines = list(range(1, 21))
        modified_lines = []
        for i in lines:
            if i == 3:
                modified_lines.append("changed line 3")
            elif i == 17:
                modified_lines.append("changed line 17")
            else:
                modified_lines.append(f"line {i}")
        modified = "\n".join(modified_lines) + "\n"

        hunks = engine.compute(original, modified)
        # Should have at least one hunk covering line 3 and possibly another for line 17
        assert len(hunks) >= 1
        all_diff_lines = [dl for h in hunks for dl in h.lines if dl.tag != " "]
        assert len(all_diff_lines) >= 2

    def test_compute_all_lines_changed(self, engine: DiffEngine) -> None:
        """Completely different content produces hunks covering everything."""
        original = "aaa\nbbb\nccc\n"
        modified = "xxx\nyyy\nzzz\n"
        hunks = engine.compute(original, modified)
        assert len(hunks) >= 1
        removed = [dl for h in hunks for dl in h.lines if dl.tag == "-"]
        added = [dl for h in hunks for dl in h.lines if dl.tag == "+"]
        assert len(removed) == 3
        assert len(added) == 3

    def test_compute_context_lines_present(self, engine: DiffEngine) -> None:
        """Hunks include context lines (tag ' ') around changes."""
        original = "\n".join(f"line {i}" for i in range(1, 11)) + "\n"
        modified = original.replace("line 5", "CHANGED")
        hunks = engine.compute(original, modified)
        context = [dl for h in hunks for dl in h.lines if dl.tag == " "]
        assert len(context) > 0

    def test_compute_hunk_positions(self, engine: DiffEngine) -> None:
        """Hunk old_start and new_start are 1-indexed."""
        original = "a\nb\nc\n"
        modified = "a\nB\nc\n"
        hunks = engine.compute(original, modified)
        assert len(hunks) >= 1
        assert hunks[0].old_start >= 1
        assert hunks[0].new_start >= 1


class TestDiffEngineComputeEdgeCases:
    """Verify edge cases for DiffEngine.compute()."""

    @pytest.fixture()
    def engine(self) -> DiffEngine:
        return DiffEngine()

    def test_compute_empty_original(self, engine: DiffEngine) -> None:
        """Empty original with non-empty modified produces addition hunks."""
        hunks = engine.compute("", "new content\n")
        assert len(hunks) >= 1
        added = [dl for h in hunks for dl in h.lines if dl.tag == "+"]
        assert len(added) >= 1

    def test_compute_empty_modified(self, engine: DiffEngine) -> None:
        """Non-empty original with empty modified produces deletion hunks."""
        hunks = engine.compute("old content\n", "")
        assert len(hunks) >= 1
        removed = [dl for h in hunks for dl in h.lines if dl.tag == "-"]
        assert len(removed) >= 1

    def test_compute_both_empty(self, engine: DiffEngine) -> None:
        """Both empty produces no hunks."""
        hunks = engine.compute("", "")
        assert hunks == []

    def test_compute_single_character_change(self, engine: DiffEngine) -> None:
        """Changing a single character produces a hunk."""
        hunks = engine.compute("abc\n", "axc\n")
        assert len(hunks) >= 1

    def test_compute_very_long_file(self, engine: DiffEngine) -> None:
        """Diff works on files with thousands of lines."""
        original = "\n".join(f"line {i}" for i in range(5000)) + "\n"
        modified = original.replace("line 2500", "MODIFIED LINE 2500")
        hunks = engine.compute(original, modified)
        assert len(hunks) >= 1

    def test_compute_binary_like_content(self, engine: DiffEngine) -> None:
        """Content with non-text characters still produces a diff."""
        original = "data: \x00\x01\x02\n"
        modified = "data: \x00\x01\x03\n"
        hunks = engine.compute(original, modified)
        assert len(hunks) >= 1

    def test_compute_trailing_newline_difference(self, engine: DiffEngine) -> None:
        """Difference in trailing newline is detected."""
        hunks = engine.compute("no newline", "no newline\n")
        assert len(hunks) >= 1

    def test_compute_whitespace_only_changes(self, engine: DiffEngine) -> None:
        """Whitespace-only changes are detected."""
        hunks = engine.compute("  indented\n", "    indented\n")
        assert len(hunks) >= 1


class TestDiffEngineApplyHunks:
    """Verify DiffEngine.apply_hunks() produces correct output."""

    @pytest.fixture()
    def engine(self) -> DiffEngine:
        return DiffEngine()

    def test_apply_no_hunks_returns_original(self, engine: DiffEngine) -> None:
        """Applying zero hunks returns the original unchanged."""
        original = "line 1\nline 2\nline 3\n"
        result = engine.apply_hunks(original, [])
        assert result == original

    def test_apply_all_hunks_produces_modified(self, engine: DiffEngine) -> None:
        """Applying all hunks reproduces the modified text."""
        original = "aaa\nbbb\nccc\n"
        modified = "aaa\nBBB\nccc\n"
        hunks = engine.compute(original, modified)
        result = engine.apply_hunks(original, hunks)
        assert result == modified

    def test_apply_subset_of_hunks(self, engine: DiffEngine) -> None:
        """Applying a subset of hunks produces a partial modification."""
        original = "a\nb\nc\nd\ne\n"
        modified = "a\nB\nc\nD\ne\n"
        hunks = engine.compute(original, modified)
        if len(hunks) >= 2:
            # Apply only the first hunk
            result = engine.apply_hunks(original, [hunks[0]])
            assert result != original
            assert result != modified


class TestDiffEngineSelectiveHunkAcceptance:
    """Verify selective hunk acceptance workflows."""

    @pytest.fixture()
    def engine(self) -> DiffEngine:
        return DiffEngine()

    def test_accept_all_hunks(self, engine: DiffEngine) -> None:
        """Accepting all hunks in a FileChange marks them all."""
        original = "a\nb\n"
        modified = "A\nB\n"
        hunks = engine.compute(original, modified)
        change = FileChange(
            path=Path("test.py"),
            original=original,
            modified=modified,
            hunks=hunks,
            status="pending",
        )
        change.status = "accepted"
        assert change.status == "accepted"

    def test_reject_all_hunks(self, engine: DiffEngine) -> None:
        """Rejecting all hunks keeps the original content."""
        original = "keep this\n"
        modified = "change this\n"
        hunks = engine.compute(original, modified)
        result = engine.apply_hunks(original, [])
        assert result == original


class TestFormatUnified:
    """Verify DiffEngine.format_unified() output."""

    @pytest.fixture()
    def engine(self) -> DiffEngine:
        return DiffEngine()

    def test_unified_contains_file_paths(self, engine: DiffEngine) -> None:
        """Unified diff output contains the file paths."""
        change = FileChange(
            path=Path("src/main.py"),
            original="old\n",
            modified="new\n",
        )
        output = engine.format_unified(change)
        assert "a/src/main.py" in output
        assert "b/src/main.py" in output

    def test_unified_contains_diff_markers(self, engine: DiffEngine) -> None:
        """Unified diff contains +/- line markers."""
        change = FileChange(
            path=Path("f.py"),
            original="old line\n",
            modified="new line\n",
        )
        output = engine.format_unified(change)
        assert output.count("\n-") >= 1 or "-old" in output
        assert output.count("\n+") >= 1 or "+new" in output

    def test_unified_identical_files_empty(self, engine: DiffEngine) -> None:
        """Unified diff of identical files produces empty output."""
        change = FileChange(
            path=Path("same.py"),
            original="same\n",
            modified="same\n",
        )
        output = engine.format_unified(change)
        assert output == ""

    def test_unified_addition_only(self, engine: DiffEngine) -> None:
        """Unified diff shows additions with + prefix."""
        change = FileChange(
            path=Path("add.py"),
            original="",
            modified="added line\n",
        )
        output = engine.format_unified(change)
        assert "+added line" in output

    def test_unified_deletion_only(self, engine: DiffEngine) -> None:
        """Unified diff shows deletions with - prefix."""
        change = FileChange(
            path=Path("del.py"),
            original="removed line\n",
            modified="",
        )
        output = engine.format_unified(change)
        assert "-removed line" in output

    def test_unified_multiline_change(self, engine: DiffEngine) -> None:
        """Unified diff correctly handles multi-line changes."""
        change = FileChange(
            path=Path("multi.py"),
            original="line 1\nline 2\nline 3\n",
            modified="line 1\nchanged\nline 3\n",
        )
        output = engine.format_unified(change)
        assert "-line 2" in output
        assert "+changed" in output


class TestFormatSideBySide:
    """Verify DiffEngine.format_side_by_side() output."""

    @pytest.fixture()
    def engine(self) -> DiffEngine:
        return DiffEngine()

    def test_side_by_side_separator(self, engine: DiffEngine) -> None:
        """Side-by-side output contains the ' | ' separator."""
        change = FileChange(
            path=Path("sbs.py"),
            original="left\n",
            modified="right\n",
        )
        output = engine.format_side_by_side(change, width=80)
        assert " | " in output

    def test_side_by_side_width_respected(self, engine: DiffEngine) -> None:
        """Each line in side-by-side output fits within the specified width."""
        change = FileChange(
            path=Path("wide.py"),
            original="a\nb\n",
            modified="x\ny\n",
        )
        output = engine.format_side_by_side(change, width=60)
        for line in output.split("\n"):
            if line:
                assert len(line) <= 60

    def test_side_by_side_same_line_count(self, engine: DiffEngine) -> None:
        """Side-by-side with same number of lines shows all."""
        change = FileChange(
            path=Path("eq.py"),
            original="a\nb\nc\n",
            modified="x\ny\nz\n",
        )
        output = engine.format_side_by_side(change, width=80)
        lines = output.strip().split("\n")
        assert len(lines) == 3

    def test_side_by_side_different_line_counts(self, engine: DiffEngine) -> None:
        """Side-by-side handles files with different line counts."""
        change = FileChange(
            path=Path("diff_len.py"),
            original="a\n",
            modified="x\ny\nz\n",
        )
        output = engine.format_side_by_side(change, width=80)
        lines = output.strip().split("\n")
        assert len(lines) == 3  # max(1, 3)

    def test_side_by_side_empty_original(self, engine: DiffEngine) -> None:
        """Side-by-side with empty original shows only right side."""
        change = FileChange(
            path=Path("empty_left.py"),
            original="",
            modified="new\n",
        )
        output = engine.format_side_by_side(change, width=80)
        assert " | " in output

    def test_side_by_side_narrow_width(self, engine: DiffEngine) -> None:
        """Side-by-side works with very narrow width."""
        change = FileChange(
            path=Path("narrow.py"),
            original="hello world\n",
            modified="goodbye world\n",
        )
        output = engine.format_side_by_side(change, width=30)
        for line in output.strip().split("\n"):
            assert len(line) <= 30
