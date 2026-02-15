"""Tests for sdk.tui.file_ops — Safe file read/write/backup/restore.

Covers FileOps.read(), write(), backup(), restore(), backup directory
creation, and path traversal prevention.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Inline stubs — mirrors sdk/tui/file_ops.py from PLAN_TUI.md
# ---------------------------------------------------------------------------

@dataclass
class FileChange:
    """A file modification tracked by FileOps."""
    path: Path
    original: str
    modified: str
    status: str = "pending"


class PathTraversalError(Exception):
    """Raised when a path escapes the allowed working directory."""


class FileOps:
    """Safe file operations with backup for Code mode."""

    def __init__(self, cwd: Path) -> None:
        self.cwd = cwd.resolve()
        self.backup_dir = self.cwd / ".obscura_backups"

    def _resolve_safe(self, path: str) -> Path:
        """Resolve a path, raising if it escapes cwd."""
        resolved = (self.cwd / path).resolve()
        if not str(resolved).startswith(str(self.cwd)):
            raise PathTraversalError(
                f"Path '{path}' resolves outside working directory: {resolved}"
            )
        return resolved

    def read(self, path: str) -> str:
        """Read a file relative to cwd. Raises FileNotFoundError if missing."""
        resolved = self._resolve_safe(path)
        if not resolved.exists():
            raise FileNotFoundError(f"File not found: {resolved}")
        return resolved.read_text()

    def write(self, path: str, content: str) -> FileChange:
        """Write content to a file, returning a FileChange with original/modified.

        Creates parent directories as needed. Records the original content
        (empty string if the file didn't exist).
        """
        resolved = self._resolve_safe(path)
        original = resolved.read_text() if resolved.exists() else ""
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content)
        return FileChange(
            path=resolved.relative_to(self.cwd),
            original=original,
            modified=content,
        )

    def backup(self, path: str) -> Path:
        """Create a backup of a file in .obscura_backups/.

        Preserves directory structure within the backup dir.
        Returns the backup file path.
        """
        resolved = self._resolve_safe(path)
        if not resolved.exists():
            raise FileNotFoundError(f"Cannot backup non-existent file: {resolved}")

        self.backup_dir.mkdir(parents=True, exist_ok=True)
        relative = resolved.relative_to(self.cwd)
        backup_path = self.backup_dir / relative
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        backup_path.write_text(resolved.read_text())
        return backup_path

    def restore(self, path: str) -> None:
        """Restore a file from .obscura_backups/.

        Raises FileNotFoundError if no backup exists.
        """
        resolved = self._resolve_safe(path)
        relative = resolved.relative_to(self.cwd)
        backup_path = self.backup_dir / relative
        if not backup_path.exists():
            raise FileNotFoundError(f"No backup found for: {relative}")
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(backup_path.read_text())


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestFileOpsRead:
    """Verify FileOps.read() for existing and missing files."""

    def test_read_existing_file(self, tmp_path: Path) -> None:
        """read() returns the content of an existing file."""
        (tmp_path / "hello.txt").write_text("hello world")
        ops = FileOps(cwd=tmp_path)
        assert ops.read("hello.txt") == "hello world"

    def test_read_missing_file_raises(self, tmp_path: Path) -> None:
        """read() raises FileNotFoundError for a missing file."""
        ops = FileOps(cwd=tmp_path)
        with pytest.raises(FileNotFoundError):
            ops.read("nonexistent.txt")

    def test_read_nested_file(self, tmp_path: Path) -> None:
        """read() works with nested directory paths."""
        nested = tmp_path / "src" / "lib"
        nested.mkdir(parents=True)
        (nested / "mod.py").write_text("# module")
        ops = FileOps(cwd=tmp_path)
        assert ops.read("src/lib/mod.py") == "# module"

    def test_read_empty_file(self, tmp_path: Path) -> None:
        """read() returns empty string for an empty file."""
        (tmp_path / "empty.txt").write_text("")
        ops = FileOps(cwd=tmp_path)
        assert ops.read("empty.txt") == ""

    def test_read_file_with_unicode(self, tmp_path: Path) -> None:
        """read() handles unicode content correctly."""
        content = "cafe\u0301 \u2603 \U0001f600"
        (tmp_path / "unicode.txt").write_text(content)
        ops = FileOps(cwd=tmp_path)
        assert ops.read("unicode.txt") == content

    def test_read_multiline_file(self, tmp_path: Path) -> None:
        """read() preserves newlines and multiline content."""
        content = "line1\nline2\nline3\n"
        (tmp_path / "multi.txt").write_text(content)
        ops = FileOps(cwd=tmp_path)
        assert ops.read("multi.txt") == content


class TestFileOpsWrite:
    """Verify FileOps.write() creates FileChange with correct fields."""

    def test_write_new_file(self, tmp_path: Path) -> None:
        """write() creates a new file and returns FileChange with empty original."""
        ops = FileOps(cwd=tmp_path)
        change = ops.write("new.py", "print('hello')")
        assert change.original == ""
        assert change.modified == "print('hello')"
        assert (tmp_path / "new.py").read_text() == "print('hello')"

    def test_write_existing_file(self, tmp_path: Path) -> None:
        """write() records the original content when overwriting."""
        (tmp_path / "exist.py").write_text("old content")
        ops = FileOps(cwd=tmp_path)
        change = ops.write("exist.py", "new content")
        assert change.original == "old content"
        assert change.modified == "new content"
        assert (tmp_path / "exist.py").read_text() == "new content"

    def test_write_returns_relative_path(self, tmp_path: Path) -> None:
        """write() returns a FileChange with path relative to cwd."""
        ops = FileOps(cwd=tmp_path)
        change = ops.write("src/main.py", "# code")
        assert change.path == Path("src/main.py")

    def test_write_creates_parent_directories(self, tmp_path: Path) -> None:
        """write() creates parent directories that don't exist."""
        ops = FileOps(cwd=tmp_path)
        ops.write("deep/nested/dir/file.py", "content")
        assert (tmp_path / "deep" / "nested" / "dir" / "file.py").exists()

    def test_write_default_status_pending(self, tmp_path: Path) -> None:
        """write() returns a FileChange with 'pending' status."""
        ops = FileOps(cwd=tmp_path)
        change = ops.write("f.py", "x")
        assert change.status == "pending"

    def test_write_empty_content(self, tmp_path: Path) -> None:
        """write() can write empty content."""
        ops = FileOps(cwd=tmp_path)
        change = ops.write("empty.py", "")
        assert change.modified == ""
        assert (tmp_path / "empty.py").read_text() == ""

    def test_write_large_content(self, tmp_path: Path) -> None:
        """write() handles large files."""
        ops = FileOps(cwd=tmp_path)
        large_content = "x" * 1_000_000
        change = ops.write("large.bin", large_content)
        assert len(change.modified) == 1_000_000


class TestFileOpsBackup:
    """Verify FileOps.backup() creates backups in .obscura_backups/."""

    def test_backup_creates_backup_file(self, tmp_path: Path) -> None:
        """backup() copies the file to .obscura_backups/."""
        (tmp_path / "src.py").write_text("original code")
        ops = FileOps(cwd=tmp_path)
        backup_path = ops.backup("src.py")
        assert backup_path.exists()
        assert backup_path.read_text() == "original code"

    def test_backup_preserves_directory_structure(self, tmp_path: Path) -> None:
        """backup() preserves the relative directory structure."""
        nested = tmp_path / "pkg" / "sub"
        nested.mkdir(parents=True)
        (nested / "mod.py").write_text("module")
        ops = FileOps(cwd=tmp_path)
        backup_path = ops.backup("pkg/sub/mod.py")
        expected = tmp_path / ".obscura_backups" / "pkg" / "sub" / "mod.py"
        assert backup_path == expected
        assert backup_path.read_text() == "module"

    def test_backup_creates_backup_dir_if_missing(self, tmp_path: Path) -> None:
        """backup() creates the .obscura_backups/ directory if needed."""
        (tmp_path / "file.txt").write_text("data")
        ops = FileOps(cwd=tmp_path)
        assert not ops.backup_dir.exists()
        ops.backup("file.txt")
        assert ops.backup_dir.exists()
        assert ops.backup_dir.is_dir()

    def test_backup_nonexistent_file_raises(self, tmp_path: Path) -> None:
        """backup() raises FileNotFoundError for missing files."""
        ops = FileOps(cwd=tmp_path)
        with pytest.raises(FileNotFoundError, match="Cannot backup"):
            ops.backup("ghost.py")

    def test_backup_overwrites_previous_backup(self, tmp_path: Path) -> None:
        """backup() overwrites an existing backup of the same file."""
        (tmp_path / "f.py").write_text("version 1")
        ops = FileOps(cwd=tmp_path)
        ops.backup("f.py")

        (tmp_path / "f.py").write_text("version 2")
        backup_path = ops.backup("f.py")
        assert backup_path.read_text() == "version 2"

    def test_backup_dir_path(self, tmp_path: Path) -> None:
        """backup_dir is at cwd/.obscura_backups."""
        ops = FileOps(cwd=tmp_path)
        assert ops.backup_dir == tmp_path.resolve() / ".obscura_backups"

    def test_multiple_file_backups(self, tmp_path: Path) -> None:
        """Multiple files can be backed up independently."""
        (tmp_path / "a.py").write_text("aaa")
        (tmp_path / "b.py").write_text("bbb")
        ops = FileOps(cwd=tmp_path)
        ops.backup("a.py")
        ops.backup("b.py")
        assert (ops.backup_dir / "a.py").read_text() == "aaa"
        assert (ops.backup_dir / "b.py").read_text() == "bbb"


class TestFileOpsRestore:
    """Verify FileOps.restore() restores from backup."""

    def test_restore_from_backup(self, tmp_path: Path) -> None:
        """restore() brings back the backed-up content."""
        (tmp_path / "f.py").write_text("original")
        ops = FileOps(cwd=tmp_path)
        ops.backup("f.py")

        (tmp_path / "f.py").write_text("modified")
        ops.restore("f.py")
        assert (tmp_path / "f.py").read_text() == "original"

    def test_restore_no_backup_raises(self, tmp_path: Path) -> None:
        """restore() raises FileNotFoundError when no backup exists."""
        (tmp_path / "f.py").write_text("content")
        ops = FileOps(cwd=tmp_path)
        with pytest.raises(FileNotFoundError, match="No backup found"):
            ops.restore("f.py")

    def test_restore_creates_parent_dirs(self, tmp_path: Path) -> None:
        """restore() creates parent directories if they were deleted."""
        nested = tmp_path / "pkg" / "sub"
        nested.mkdir(parents=True)
        (nested / "mod.py").write_text("saved")
        ops = FileOps(cwd=tmp_path)
        ops.backup("pkg/sub/mod.py")

        # Remove the original directory structure
        import shutil
        shutil.rmtree(tmp_path / "pkg")

        ops.restore("pkg/sub/mod.py")
        assert (nested / "mod.py").read_text() == "saved"

    def test_restore_nested_path(self, tmp_path: Path) -> None:
        """restore() works with nested directory structures."""
        deep = tmp_path / "a" / "b" / "c"
        deep.mkdir(parents=True)
        (deep / "d.py").write_text("deep content")
        ops = FileOps(cwd=tmp_path)
        ops.backup("a/b/c/d.py")

        (deep / "d.py").write_text("changed")
        ops.restore("a/b/c/d.py")
        assert (deep / "d.py").read_text() == "deep content"

    def test_backup_then_modify_then_restore_roundtrip(self, tmp_path: Path) -> None:
        """Full backup -> modify -> restore roundtrip preserves original."""
        (tmp_path / "config.json").write_text('{"key": "value"}')
        ops = FileOps(cwd=tmp_path)

        # Backup original
        ops.backup("config.json")

        # Modify the file
        ops.write("config.json", '{"key": "new_value"}')
        assert (tmp_path / "config.json").read_text() == '{"key": "new_value"}'

        # Restore
        ops.restore("config.json")
        assert (tmp_path / "config.json").read_text() == '{"key": "value"}'


class TestPathTraversalPrevention:
    """Verify that FileOps prevents writing outside cwd."""

    def test_parent_traversal_raises(self, tmp_path: Path) -> None:
        """Paths with '..' that escape cwd raise PathTraversalError."""
        ops = FileOps(cwd=tmp_path)
        with pytest.raises(PathTraversalError):
            ops.read("../../etc/passwd")

    def test_absolute_path_outside_cwd_raises(self, tmp_path: Path) -> None:
        """Absolute paths outside cwd raise PathTraversalError."""
        ops = FileOps(cwd=tmp_path)
        with pytest.raises(PathTraversalError):
            ops.read("/etc/hosts")

    def test_write_parent_traversal_raises(self, tmp_path: Path) -> None:
        """write() with parent traversal raises PathTraversalError."""
        ops = FileOps(cwd=tmp_path)
        with pytest.raises(PathTraversalError):
            ops.write("../escape.txt", "evil")

    def test_backup_parent_traversal_raises(self, tmp_path: Path) -> None:
        """backup() with parent traversal raises PathTraversalError."""
        ops = FileOps(cwd=tmp_path)
        with pytest.raises(PathTraversalError):
            ops.backup("../../../etc/shadow")

    def test_restore_parent_traversal_raises(self, tmp_path: Path) -> None:
        """restore() with parent traversal raises PathTraversalError."""
        ops = FileOps(cwd=tmp_path)
        with pytest.raises(PathTraversalError):
            ops.restore("../../tmp/exploit")

    def test_symlink_escape_raises(self, tmp_path: Path) -> None:
        """Symlink pointing outside cwd raises PathTraversalError."""
        target = Path("/tmp")
        link = tmp_path / "escape_link"
        try:
            link.symlink_to(target)
        except OSError:
            pytest.skip("Cannot create symlinks in this environment")
        ops = FileOps(cwd=tmp_path)
        with pytest.raises(PathTraversalError):
            ops.read("escape_link/somefile")

    def test_allowed_path_within_cwd(self, tmp_path: Path) -> None:
        """Paths within cwd are allowed even with '..' that stay inside."""
        subdir = tmp_path / "sub" / "deep"
        subdir.mkdir(parents=True)
        (tmp_path / "sub" / "target.txt").write_text("ok")
        ops = FileOps(cwd=tmp_path)
        content = ops.read("sub/deep/../target.txt")
        assert content == "ok"

    def test_dot_path_allowed(self, tmp_path: Path) -> None:
        """Path starting with './' is allowed when it stays in cwd."""
        (tmp_path / "file.txt").write_text("dot path")
        ops = FileOps(cwd=tmp_path)
        assert ops.read("./file.txt") == "dot path"

    def test_deeply_nested_traversal_raises(self, tmp_path: Path) -> None:
        """Deeply nested '..' traversal that escapes cwd raises."""
        ops = FileOps(cwd=tmp_path)
        with pytest.raises(PathTraversalError):
            ops.read("a/b/c/../../../../etc/passwd")


class TestFileOpsEdgeCases:
    """Additional edge cases for FileOps."""

    def test_cwd_is_resolved(self, tmp_path: Path) -> None:
        """FileOps resolves cwd to an absolute path."""
        ops = FileOps(cwd=tmp_path)
        assert ops.cwd.is_absolute()
        assert ops.cwd == tmp_path.resolve()

    def test_write_then_read_roundtrip(self, tmp_path: Path) -> None:
        """write() then read() returns the written content."""
        ops = FileOps(cwd=tmp_path)
        ops.write("roundtrip.txt", "hello there")
        assert ops.read("roundtrip.txt") == "hello there"

    def test_multiple_writes_to_same_file(self, tmp_path: Path) -> None:
        """Multiple writes to the same file track original from each write."""
        ops = FileOps(cwd=tmp_path)
        change1 = ops.write("f.txt", "version 1")
        assert change1.original == ""

        change2 = ops.write("f.txt", "version 2")
        assert change2.original == "version 1"
        assert change2.modified == "version 2"

    def test_backup_dir_not_created_until_needed(self, tmp_path: Path) -> None:
        """The .obscura_backups/ directory is not created until backup() is called."""
        ops = FileOps(cwd=tmp_path)
        ops.write("f.txt", "content")
        assert not ops.backup_dir.exists()
