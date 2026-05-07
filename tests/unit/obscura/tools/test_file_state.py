"""Unit tests for file_state — read/staleness/history tracking.

All functions are sync and operate on module-level dicts.
An autouse fixture calls clear() before and after each test to
prevent state leakage between tests.

No mocks needed — tests use tmp_path files and os.utime() to force
mtime changes that are reliable across all filesystems.
"""
from __future__ import annotations

import os
import time
from collections.abc import Generator
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Autouse: reset module state between tests
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_state() -> Generator[None, None, None]:
    from obscura.tools.system.file_state import clear

    clear()
    yield
    clear()


# ---------------------------------------------------------------------------
# record_read / check_staleness
# ---------------------------------------------------------------------------


def test_check_staleness_no_prior_read_returns_none(tmp_path: Path) -> None:
    from obscura.tools.system.file_state import check_staleness

    f = tmp_path / "file.txt"
    f.write_text("hello")

    # No record_read call → backwards-compat: no staleness error
    assert check_staleness(f) is None


def test_record_read_then_check_staleness_no_change_returns_none(
    tmp_path: Path,
) -> None:
    from obscura.tools.system.file_state import check_staleness, record_read

    f = tmp_path / "file.txt"
    f.write_text("hello")
    record_read(f)

    assert check_staleness(f) is None


def test_check_staleness_after_modification_returns_error_string(
    tmp_path: Path,
) -> None:
    from obscura.tools.system.file_state import check_staleness, record_read

    f = tmp_path / "file.txt"
    f.write_text("original")
    record_read(f)

    # Force a future mtime regardless of filesystem precision
    future = time.time() + 10
    os.utime(str(f), (future, future))

    result = check_staleness(f)
    assert result is not None
    assert "modified externally" in result


def test_check_staleness_missing_file_returns_none(tmp_path: Path) -> None:
    """OSError on stat() is suppressed — no false staleness on deleted files."""
    from obscura.tools.system.file_state import check_staleness, record_read

    f = tmp_path / "file.txt"
    f.write_text("x")
    record_read(f)
    f.unlink()

    assert check_staleness(f) is None


def test_record_read_nonexistent_file_is_silently_ignored(tmp_path: Path) -> None:
    """record_read on a missing file must not raise."""
    from obscura.tools.system.file_state import record_read

    record_read(tmp_path / "ghost.txt")  # should not raise


# ---------------------------------------------------------------------------
# is_unchanged
# ---------------------------------------------------------------------------


def test_is_unchanged_returns_false_without_prior_read(tmp_path: Path) -> None:
    from obscura.tools.system.file_state import is_unchanged

    f = tmp_path / "file.txt"
    f.write_text("x")

    assert is_unchanged(f) is False


def test_is_unchanged_returns_true_after_record_read(tmp_path: Path) -> None:
    from obscura.tools.system.file_state import is_unchanged, record_read

    f = tmp_path / "file.txt"
    f.write_text("x")
    record_read(f, offset=None, limit=None)

    assert is_unchanged(f, offset=None, limit=None) is True


def test_is_unchanged_returns_false_after_modification(tmp_path: Path) -> None:
    from obscura.tools.system.file_state import is_unchanged, record_read

    f = tmp_path / "file.txt"
    f.write_text("x")
    record_read(f)

    future = time.time() + 10
    os.utime(str(f), (future, future))

    assert is_unchanged(f) is False


def test_is_unchanged_different_offset_is_independent(tmp_path: Path) -> None:
    """A read recorded with offset=0 does not satisfy is_unchanged(offset=10)."""
    from obscura.tools.system.file_state import is_unchanged, record_read

    f = tmp_path / "file.txt"
    f.write_text("x")
    record_read(f, offset=0, limit=None)

    assert is_unchanged(f, offset=10, limit=None) is False


# ---------------------------------------------------------------------------
# record_file_access / get_file_history
# ---------------------------------------------------------------------------


def test_record_file_access_appended_to_history(tmp_path: Path) -> None:
    from obscura.tools.system.file_state import get_file_history, record_file_access

    f = tmp_path / "file.txt"
    record_file_access(f, "read")

    history = get_file_history()
    assert len(history) == 1
    _, action, path = history[0]
    assert action == "read"
    assert path == str(f.resolve())


def test_get_file_history_limit_caps_results(tmp_path: Path) -> None:
    from obscura.tools.system.file_state import get_file_history, record_file_access

    f = tmp_path / "file.txt"
    for _ in range(5):
        record_file_access(f, "read")

    assert len(get_file_history(limit=2)) == 2


# ---------------------------------------------------------------------------
# get_recently_modified_files
# ---------------------------------------------------------------------------


def test_get_recently_modified_files_includes_writes(tmp_path: Path) -> None:
    from obscura.tools.system.file_state import (
        get_recently_modified_files,
        record_file_access,
    )

    f = tmp_path / "file.txt"
    record_file_access(f, "write")

    assert str(f.resolve()) in get_recently_modified_files()


def test_get_recently_modified_files_excludes_reads(tmp_path: Path) -> None:
    from obscura.tools.system.file_state import (
        get_recently_modified_files,
        record_file_access,
    )

    f = tmp_path / "file.txt"
    record_file_access(f, "read")

    assert get_recently_modified_files() == []


def test_get_recently_modified_files_deduplicates(tmp_path: Path) -> None:
    from obscura.tools.system.file_state import (
        get_recently_modified_files,
        record_file_access,
    )

    f = tmp_path / "file.txt"
    record_file_access(f, "write")
    record_file_access(f, "edit")

    result = get_recently_modified_files()
    assert result.count(str(f.resolve())) == 1


# ---------------------------------------------------------------------------
# get_recently_read_files
# ---------------------------------------------------------------------------


def test_get_recently_read_files_includes_reads(tmp_path: Path) -> None:
    from obscura.tools.system.file_state import (
        get_recently_read_files,
        record_file_access,
    )

    f = tmp_path / "file.txt"
    record_file_access(f, "read")

    assert str(f.resolve()) in get_recently_read_files()


def test_get_recently_read_files_deduplicates(tmp_path: Path) -> None:
    from obscura.tools.system.file_state import (
        get_recently_read_files,
        record_file_access,
    )

    f = tmp_path / "file.txt"
    record_file_access(f, "read")
    record_file_access(f, "read")

    result = get_recently_read_files()
    assert result.count(str(f.resolve())) == 1


# ---------------------------------------------------------------------------
# clear
# ---------------------------------------------------------------------------


def test_clear_resets_all_state(tmp_path: Path) -> None:
    from obscura.tools.system.file_state import (
        clear,
        get_file_history,
        get_recently_modified_files,
        get_recently_read_files,
        is_unchanged,
        record_file_access,
        record_read,
    )

    f = tmp_path / "file.txt"
    f.write_text("x")
    record_read(f)
    record_file_access(f, "write")

    clear()

    assert get_file_history() == []
    assert get_recently_modified_files() == []
    assert get_recently_read_files() == []
    assert is_unchanged(f) is False  # dedup cache cleared
