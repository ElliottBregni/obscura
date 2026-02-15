"""
sdk.tui.file_ops -- Safe file operations with backup and restore.

All file writes in Code mode go through FileOps, which creates
backups in ``.obscura_backups/`` before overwriting files. Supports
restore to undo changes.
"""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from sdk.tui.diff_engine import DiffEngine, FileChange


class FileOps:
    """Safe file operations with backup support.

    All paths are resolved relative to the working directory (``cwd``).
    Backups are stored in ``<cwd>/.obscura_backups/<relative_path>.<timestamp>``.
    """

    def __init__(self, cwd: Path | str | None = None) -> None:
        self.cwd: Path = Path(cwd or ".").resolve()
        self.backup_dir: Path = self.cwd / ".obscura_backups"
        self._diff_engine = DiffEngine()

    # -- Path resolution ----------------------------------------------------

    def _resolve(self, path: str) -> Path:
        """Resolve a path relative to cwd.

        Raises:
            ValueError: If the resolved path escapes the cwd directory.
        """
        resolved = (self.cwd / path).resolve()

        # Security: prevent path traversal
        try:
            resolved.relative_to(self.cwd)
        except ValueError:
            raise ValueError(f"Path escapes working directory: {path}")

        return resolved

    # -- Read ---------------------------------------------------------------

    def read(self, path: str) -> str:
        """Read a file relative to cwd.

        Args:
            path: Relative path from cwd.

        Returns:
            The file content as a string.

        Raises:
            FileNotFoundError: If the file does not exist.
            ValueError: If the path escapes cwd.
        """
        resolved = self._resolve(path)
        if not resolved.exists():
            raise FileNotFoundError(f"File not found: {path}")
        if not resolved.is_file():
            raise ValueError(f"Not a file: {path}")
        return resolved.read_text(encoding="utf-8", errors="replace")

    # -- Write --------------------------------------------------------------

    def write(self, path: str, content: str) -> FileChange:
        """Write to a file, creating a backup first if the file exists.

        Args:
            path: Relative path from cwd.
            content: The new file content.

        Returns:
            A FileChange with the original (if any) and new content,
            plus computed diff hunks.

        Raises:
            ValueError: If the path escapes cwd.
        """
        resolved = self._resolve(path)

        # Read original for diff
        original = ""
        if resolved.exists():
            original = resolved.read_text(encoding="utf-8", errors="replace")
            # Create backup before modifying
            self.backup(path)

        # Ensure parent directory exists
        resolved.parent.mkdir(parents=True, exist_ok=True)

        # Write the new content
        resolved.write_text(content, encoding="utf-8")

        # Compute diff
        return self._diff_engine.compute_change(
            path=Path(path),
            original=original,
            modified=content,
        )

    # -- Backup -------------------------------------------------------------

    def backup(self, path: str) -> Path:
        """Create a timestamped backup of a file.

        Args:
            path: Relative path from cwd.

        Returns:
            The path to the backup file.

        Raises:
            FileNotFoundError: If the file does not exist.
            ValueError: If the path escapes cwd.
        """
        resolved = self._resolve(path)
        if not resolved.exists():
            raise FileNotFoundError(f"Cannot backup: file not found: {path}")

        # Build backup path preserving directory structure
        rel = resolved.relative_to(self.cwd)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = self.backup_dir / f"{rel}.{ts}"

        backup_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(resolved), str(backup_path))

        return backup_path

    # -- Restore ------------------------------------------------------------

    def restore(self, path: str) -> None:
        """Restore a file from the most recent backup.

        Args:
            path: Relative path from cwd.

        Raises:
            FileNotFoundError: If no backup exists for this file.
            ValueError: If the path escapes cwd.
        """
        resolved = self._resolve(path)
        rel = resolved.relative_to(self.cwd)

        # Find the most recent backup
        backup_pattern = f"{rel}.*"
        backups = sorted(
            self.backup_dir.glob(backup_pattern),
            reverse=True,
        )

        if not backups:
            raise FileNotFoundError(f"No backup found for: {path}")

        latest = backups[0]
        shutil.copy2(str(latest), str(resolved))

    # -- List backups -------------------------------------------------------

    def list_backups(self, path: str | None = None) -> list[dict[str, Any]]:
        """List available backups, optionally filtered by path.

        Args:
            path: If given, only list backups for this file.

        Returns:
            List of dicts with 'path', 'backup_path', 'timestamp'.
        """
        if not self.backup_dir.exists():
            return []

        results: list[dict[str, Any]] = []

        if path:
            resolved = self._resolve(path)
            rel = resolved.relative_to(self.cwd)
            pattern = f"{rel}.*"
            candidates = self.backup_dir.glob(pattern)
        else:
            candidates = self.backup_dir.rglob("*")

        for backup in candidates:
            if not backup.is_file():
                continue
            # Extract timestamp from backup filename
            name = backup.name
            parts = name.rsplit(".", 1)
            if len(parts) >= 2:
                results.append(
                    {
                        "path": str(backup.relative_to(self.backup_dir)).rsplit(".", 1)[
                            0
                        ],
                        "backup_path": str(backup),
                        "timestamp": parts[-1],
                    }
                )

        results.sort(key=lambda x: x["timestamp"], reverse=True)
        return results

    # -- Cleanup ------------------------------------------------------------

    def cleanup_backups(self, max_age_days: int = 30) -> int:
        """Remove backups older than max_age_days.

        Returns:
            The number of backup files removed.
        """
        if not self.backup_dir.exists():
            return 0

        cutoff = datetime.now().timestamp() - (max_age_days * 86400)
        count = 0

        for backup in self.backup_dir.rglob("*"):
            if backup.is_file() and backup.stat().st_mtime < cutoff:
                backup.unlink()
                count += 1

        # Remove empty directories
        for d in sorted(
            self.backup_dir.rglob("*"),
            reverse=True,
        ):
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()

        return count
