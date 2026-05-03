"""obscura.core.file_watcher — Lightweight file change tracker for agent sessions.

Tracks file modifications during agent execution using polling (no external
dependencies like watchdog required).  Changes are accumulated and can be
queried at any point to see what files were created, modified, or deleted
during the session.

Usage::

    watcher = FileWatcher(["/path/to/project"])
    watcher.start()

    # ... agent runs and modifies files ...

    changes = watcher.get_changes()
    for change in changes:
        print(f"{change.kind}: {change.path}")

    watcher.stop()
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Default polling interval in seconds
DEFAULT_POLL_INTERVAL = 2.0

# Default ignore patterns (common non-project files)
DEFAULT_IGNORE_PATTERNS: frozenset[str] = frozenset(
    {
        "__pycache__",
        ".git",
        "node_modules",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".pyright",
        ".DS_Store",
        "*.pyc",
        "*.pyo",
        ".env",
    }
)


class ChangeKind(Enum):
    """Type of file change detected."""

    CREATED = "created"
    MODIFIED = "modified"
    DELETED = "deleted"


@dataclass(frozen=True)
class FileChange:
    """A single file change event."""

    kind: ChangeKind
    path: str
    timestamp: float
    size_bytes: int = 0


@dataclass
class _FileSnapshot:
    """Internal mtime+size snapshot of a file."""

    mtime: float
    size: int


class FileWatcher:
    """Polls directories for file changes during agent execution.

    Uses mtime+size comparison rather than inotify/watchdog to avoid
    external dependencies.  Suitable for tracking project-level changes
    during a single agent session (not designed for long-running daemons).
    """

    def __init__(
        self,
        watch_paths: list[str | Path],
        *,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        ignore_patterns: frozenset[str] | None = None,
        max_depth: int = 5,
    ) -> None:
        self._watch_paths = [Path(p) for p in watch_paths]
        self._poll_interval = poll_interval
        self._ignore_patterns = ignore_patterns or DEFAULT_IGNORE_PATTERNS
        self._max_depth = max_depth
        self._baseline: dict[str, _FileSnapshot] = {}
        self._changes: list[FileChange] = []
        self._lock = threading.Lock()
        self._running = False
        self._thread: threading.Thread | None = None

    def _should_ignore(self, path: Path) -> bool:
        """Check if a path matches any ignore pattern."""
        parts = path.parts
        name = path.name
        for pattern in self._ignore_patterns:
            if pattern.startswith("*"):
                if name.endswith(pattern[1:]):
                    return True
            elif pattern in parts or name == pattern:
                return True
        return False

    def _scan(self) -> dict[str, _FileSnapshot]:
        """Walk watched directories and collect mtime+size for all files."""
        snapshot: dict[str, _FileSnapshot] = {}
        for root_path in self._watch_paths:
            if not root_path.is_dir():
                continue
            try:
                for dirpath, dirnames, filenames in os.walk(
                    root_path, followlinks=False
                ):
                    dp = Path(dirpath)
                    # Depth check
                    try:
                        rel = dp.relative_to(root_path)
                        if len(rel.parts) > self._max_depth:
                            dirnames.clear()
                            continue
                    except ValueError:
                        logger.debug("suppressed exception in _scan", exc_info=True)
                        continue

                    # Prune ignored directories in-place
                    dirnames[:] = [
                        d for d in dirnames if not self._should_ignore(dp / d)
                    ]

                    for fname in filenames:
                        fpath = dp / fname
                        if self._should_ignore(fpath):
                            continue
                        try:
                            st = fpath.stat()
                            snapshot[str(fpath)] = _FileSnapshot(
                                mtime=st.st_mtime,
                                size=st.st_size,
                            )
                        except (OSError, PermissionError):
                            logger.debug("suppressed exception in _scan", exc_info=True)
                            continue
            except (OSError, PermissionError):
                logger.debug("suppressed exception in _scan", exc_info=True)
                continue
        return snapshot

    def _detect_changes(self, current: dict[str, _FileSnapshot]) -> None:
        """Compare current scan against baseline and record changes."""
        now = time.time()
        new_changes: list[FileChange] = []

        # Created or modified
        for path, snap in current.items():
            old = self._baseline.get(path)
            if old is None:
                new_changes.append(
                    FileChange(
                        kind=ChangeKind.CREATED,
                        path=path,
                        timestamp=now,
                        size_bytes=snap.size,
                    )
                )
            elif snap.mtime != old.mtime or snap.size != old.size:
                new_changes.append(
                    FileChange(
                        kind=ChangeKind.MODIFIED,
                        path=path,
                        timestamp=now,
                        size_bytes=snap.size,
                    )
                )

        # Deleted
        for path in self._baseline:
            if path not in current:
                new_changes.append(
                    FileChange(
                        kind=ChangeKind.DELETED,
                        path=path,
                        timestamp=now,
                    )
                )

        if new_changes:
            with self._lock:
                self._changes.extend(new_changes)

        # Update baseline
        self._baseline = current

    def _poll_loop(self) -> None:
        """Background polling loop."""
        while self._running:
            try:
                current = self._scan()
                self._detect_changes(current)
            except Exception:
                logger.debug("File watcher poll error", exc_info=True)
            time.sleep(self._poll_interval)

    def start(self) -> None:
        """Take baseline snapshot and start polling in a background thread."""
        if self._running:
            return
        self._baseline = self._scan()
        self._running = True
        self._thread = threading.Thread(
            target=self._poll_loop,
            daemon=True,
            name="obscura-file-watcher",
        )
        self._thread.start()
        logger.info(
            "File watcher started for %d paths (%d files baselined)",
            len(self._watch_paths),
            len(self._baseline),
        )

    def stop(self) -> None:
        """Stop the polling thread."""
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
        logger.info("File watcher stopped")

    def get_changes(self) -> list[FileChange]:
        """Return all accumulated changes since start (thread-safe)."""
        with self._lock:
            return list(self._changes)

    def get_changes_since(self, since: float) -> list[FileChange]:
        """Return changes since a given timestamp."""
        with self._lock:
            return [c for c in self._changes if c.timestamp >= since]

    def clear_changes(self) -> None:
        """Clear accumulated changes."""
        with self._lock:
            self._changes.clear()

    @property
    def is_running(self) -> bool:
        """Whether the watcher is actively polling."""
        return self._running

    @property
    def file_count(self) -> int:
        """Number of files in the current baseline."""
        return len(self._baseline)

    def summary(self) -> dict[str, Any]:
        """Return a summary of changes by kind."""
        with self._lock:
            created = sum(1 for c in self._changes if c.kind == ChangeKind.CREATED)
            modified = sum(1 for c in self._changes if c.kind == ChangeKind.MODIFIED)
            deleted = sum(1 for c in self._changes if c.kind == ChangeKind.DELETED)
            return {
                "created": created,
                "modified": modified,
                "deleted": deleted,
                "total": len(self._changes),
                "baseline_files": len(self._baseline),
            }


__all__ = [
    "ChangeKind",
    "FileChange",
    "FileWatcher",
]
