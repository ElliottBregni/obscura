"""Tests for obscura.tools.system.file_state — staleness tracking."""

from __future__ import annotations

import time
from pathlib import Path

from obscura.tools.system.file_state import (
    check_staleness,
    clear,
    is_unchanged,
    record_file_access,
    record_read,
    get_recently_modified_files,
)


def test_record_and_check_fresh(tmp_path: Path) -> None:
    clear()
    f = tmp_path / "test.txt"
    f.write_text("hello")
    record_read(f)
    assert check_staleness(f) is None


def test_check_stale_after_modification(tmp_path: Path) -> None:
    clear()
    f = tmp_path / "test.txt"
    f.write_text("v1")
    record_read(f)
    time.sleep(0.05)
    f.write_text("v2")
    result = check_staleness(f)
    assert result is not None
    assert "modified externally" in result


def test_unread_file_not_stale(tmp_path: Path) -> None:
    clear()
    f = tmp_path / "never_read.txt"
    f.write_text("data")
    assert check_staleness(f) is None


def test_is_unchanged(tmp_path: Path) -> None:
    clear()
    f = tmp_path / "test.txt"
    f.write_text("content")
    record_read(f)
    assert is_unchanged(f)
    f.write_text("changed")
    assert not is_unchanged(f)


def test_file_history_tracking(tmp_path: Path) -> None:
    clear()
    f1 = tmp_path / "a.py"
    f2 = tmp_path / "b.py"
    f1.write_text("")
    f2.write_text("")
    record_file_access(f1, "read")
    record_file_access(f2, "write")
    record_file_access(f1, "edit")
    modified = get_recently_modified_files(limit=5)
    assert str(f1.resolve()) in modified
    assert str(f2.resolve()) in modified


def test_clear_resets_all(tmp_path: Path) -> None:
    clear()
    f = tmp_path / "test.txt"
    f.write_text("data")
    record_read(f)
    record_file_access(f, "read")
    clear()
    assert not is_unchanged(f)
    assert get_recently_modified_files() == []
