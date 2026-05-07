"""Unit tests for obscura.tools.worktree_observer.

Module-level `_observers` dict is cleared in autouse fixture to prevent
cross-test contamination. `FileWatcher` is patched to avoid real filesystem
watch threads.
"""
from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import obscura.tools.worktree_observer as _obs

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clear_observers() -> Generator[None, None, None]:
    _obs._observers.clear()
    yield
    _obs._observers.clear()


def _mock_watcher() -> MagicMock:
    w = MagicMock()
    w.start.return_value = None
    w.stop.return_value = None
    w.summary.return_value = {"files_changed": 3}
    return w


# ---------------------------------------------------------------------------
# start
# ---------------------------------------------------------------------------


def test_start_path_not_directory_returns_false(tmp_path: Path) -> None:
    missing = tmp_path / "nonexistent"

    result = _obs.start("my-slug", missing)

    assert result is False


def test_start_existing_directory_starts_watcher(tmp_path: Path) -> None:
    mock_watcher = _mock_watcher()

    with patch.object(_obs, "FileWatcher", return_value=mock_watcher):
        result = _obs.start("my-slug", tmp_path)

    assert result is True
    mock_watcher.start.assert_called_once()
    assert "my-slug" in _obs.active_slugs()


def test_start_idempotent_returns_false_on_second_call(tmp_path: Path) -> None:
    mock_watcher = _mock_watcher()

    with patch.object(_obs, "FileWatcher", return_value=mock_watcher):
        _obs.start("dup-slug", tmp_path)
        result = _obs.start("dup-slug", tmp_path)

    assert result is False
    assert mock_watcher.start.call_count == 1


def test_start_watcher_exception_returns_false(tmp_path: Path) -> None:
    mock_watcher = _mock_watcher()
    mock_watcher.start.side_effect = OSError("inotify failed")

    with patch.object(_obs, "FileWatcher", return_value=mock_watcher):
        result = _obs.start("fail-slug", tmp_path)

    assert result is False
    assert "fail-slug" not in _obs.active_slugs()


# ---------------------------------------------------------------------------
# stop
# ---------------------------------------------------------------------------


def test_stop_unknown_slug_returns_false() -> None:
    assert _obs.stop("does-not-exist") is False


def test_stop_known_slug_returns_true(tmp_path: Path) -> None:
    mock_watcher = _mock_watcher()

    with patch.object(_obs, "FileWatcher", return_value=mock_watcher):
        _obs.start("removable", tmp_path)

    result = _obs.stop("removable")

    assert result is True
    mock_watcher.stop.assert_called_once()
    assert "removable" not in _obs.active_slugs()


def test_stop_watcher_exception_still_returns_true(tmp_path: Path) -> None:
    mock_watcher = _mock_watcher()
    mock_watcher.stop.side_effect = RuntimeError("crash")

    with patch.object(_obs, "FileWatcher", return_value=mock_watcher):
        _obs.start("crashy", tmp_path)

    result = _obs.stop("crashy")

    # Exception is swallowed; slug is removed from registry
    assert result is True
    assert "crashy" not in _obs.active_slugs()


# ---------------------------------------------------------------------------
# summary
# ---------------------------------------------------------------------------


def test_summary_unknown_slug_returns_none() -> None:
    assert _obs.summary("ghost") is None


def test_summary_active_slug_returns_watcher_summary(tmp_path: Path) -> None:
    mock_watcher = _mock_watcher()

    with patch.object(_obs, "FileWatcher", return_value=mock_watcher):
        _obs.start("watched", tmp_path)

    result = _obs.summary("watched")

    assert result is not None
    assert result["files_changed"] == 3


# ---------------------------------------------------------------------------
# active_slugs
# ---------------------------------------------------------------------------


def test_active_slugs_empty_when_no_observers() -> None:
    assert _obs.active_slugs() == []


def test_active_slugs_returns_all_started_slugs(tmp_path: Path) -> None:
    with patch.object(_obs, "FileWatcher", return_value=_mock_watcher()):
        _obs.start("slug-a", tmp_path)
        _obs.start("slug-b", tmp_path)

    slugs = _obs.active_slugs()
    assert "slug-a" in slugs
    assert "slug-b" in slugs


# ---------------------------------------------------------------------------
# stop_all
# ---------------------------------------------------------------------------


def test_stop_all_returns_stopped_slugs(tmp_path: Path) -> None:
    with patch.object(_obs, "FileWatcher", return_value=_mock_watcher()):
        _obs.start("wt-1", tmp_path)
        _obs.start("wt-2", tmp_path)

    stopped = _obs.stop_all()

    assert set(stopped) == {"wt-1", "wt-2"}
    assert _obs.active_slugs() == []


def test_stop_all_clears_observers_even_on_exception(tmp_path: Path) -> None:
    crashing = _mock_watcher()
    crashing.stop.side_effect = RuntimeError("crash in stop_all")

    with patch.object(_obs, "FileWatcher", return_value=crashing):
        _obs.start("broken", tmp_path)

    _obs.stop_all()

    assert _obs.active_slugs() == []
