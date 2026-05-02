"""obscura.tools.system.file_state — Read-before-edit staleness tracking.

Tracks file modification times at read time so that edit/write tools
can reject changes when the file has been modified externally since
the last read.  This prevents silent data loss from stale edits.

Pattern borrowed from claude-code's ``readFileState`` mtime validation.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

# {canonical_path_str: (read_timestamp, mtime_at_read)}
_read_state: dict[str, tuple[float, float]] = {}

# Optional dedup cache: {(path, offset, limit): mtime}
_dedup_cache: dict[tuple[str, int | None, int | None], float] = {}


def record_read(
    path: Path,
    *,
    offset: int | None = None,
    limit: int | None = None,
) -> None:
    """Record that *path* was read at the current time.

    Call this after every successful ``read_text_file`` invocation so
    that subsequent edits can verify the file has not been changed.
    """
    resolved = str(path.resolve())
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return
    _read_state[resolved] = (time.time(), mtime)
    _dedup_cache[(resolved, offset, limit)] = mtime


def check_staleness(path: Path) -> str | None:
    """Return an error message if *path* is stale, or ``None`` if safe.

    A file is considered stale when its current mtime is newer than the
    mtime recorded at the time of the last ``record_read`` call.  If no
    read has been recorded for the file, no staleness error is raised
    (backwards-compatible with the old behaviour).
    """
    resolved = str(path.resolve())
    entry = _read_state.get(resolved)
    if entry is None:
        # No prior read recorded — allow the edit (backwards compat).
        return None
    _read_ts, read_mtime = entry
    try:
        current_mtime = path.stat().st_mtime
    except OSError:
        return None
    if current_mtime > read_mtime:
        return (
            f"File was modified externally since last read "
            f"(read mtime={read_mtime:.6f}, current mtime={current_mtime:.6f}). "
            f"Re-read the file before editing."
        )
    return None


def is_unchanged(
    path: Path,
    *,
    offset: int | None = None,
    limit: int | None = None,
) -> bool:
    """Return ``True`` if *path* has not changed since the last read with
    the same (offset, limit) parameters.  Used for read deduplication.
    """
    resolved = str(path.resolve())
    key = (resolved, offset, limit)
    prev_mtime = _dedup_cache.get(key)
    if prev_mtime is None:
        return False
    try:
        return path.stat().st_mtime == prev_mtime
    except OSError:
        return False


# ---------------------------------------------------------------------------
# File history tracking — which files were read/written per session.
# ---------------------------------------------------------------------------

_file_history: list[tuple[float, str, str]] = []  # (timestamp, action, path)


def record_file_access(path: Path, action: str = "read") -> None:
    """Record a file access for history tracking.

    Actions: "read", "write", "edit", "create", "delete"
    """
    _file_history.append((time.time(), action, str(path.resolve())))


def get_file_history(*, limit: int = 100) -> list[tuple[float, str, str]]:
    """Return recent file access history."""
    return _file_history[-limit:]


def get_recently_modified_files(*, limit: int = 10) -> list[str]:
    """Return paths of recently written/edited files."""
    seen: set[str] = set()
    result: list[str] = []
    for _, action, path in reversed(_file_history):
        if action in ("write", "edit", "create") and path not in seen:
            seen.add(path)
            result.append(path)
            if len(result) >= limit:
                break
    return result


def get_recently_read_files(*, limit: int = 10) -> list[str]:
    """Return paths of recently read files."""
    seen: set[str] = set()
    result: list[str] = []
    for _, action, path in reversed(_file_history):
        if action == "read" and path not in seen:
            seen.add(path)
            result.append(path)
            if len(result) >= limit:
                break
    return result


def clear() -> None:
    """Clear all tracked state.  Useful in tests."""
    _read_state.clear()
    _dedup_cache.clear()
    _file_history.clear()
