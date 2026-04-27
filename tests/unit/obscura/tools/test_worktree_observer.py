"""Tests for obscura.tools.worktree_observer."""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path

import pytest

import obscura.tools.worktree_observer as worktree_observer


@pytest.fixture(autouse=True)
def _stop_all_after() -> Iterator[None]:
    yield
    worktree_observer.stop_all()


def test_start_refuses_nonexistent_path(tmp_path: Path) -> None:
    assert worktree_observer.start("x", tmp_path / "nope") is False
    assert worktree_observer.active_slugs() == []


def test_start_and_stop(tmp_path: Path) -> None:
    assert worktree_observer.start("s1", tmp_path) is True
    assert "s1" in worktree_observer.active_slugs()
    assert worktree_observer.stop("s1") is True
    assert worktree_observer.active_slugs() == []


def test_start_is_idempotent(tmp_path: Path) -> None:
    assert worktree_observer.start("s1", tmp_path) is True
    assert worktree_observer.start("s1", tmp_path) is False
    worktree_observer.stop("s1")


def test_stop_unknown_returns_false() -> None:
    assert worktree_observer.stop("never-started") is False


def test_summary_detects_changes(tmp_path: Path) -> None:
    worktree_observer.start("sum", tmp_path)
    # Let the watcher take a baseline, then modify a file.
    time.sleep(0.3)
    (tmp_path / "hello.txt").write_text("hi")
    # Give the 2-second poll a chance.
    deadline = time.time() + 4.0
    summary: dict[str, object] | None = None
    while time.time() < deadline:
        summary = worktree_observer.summary("sum")
        if summary is not None and int(summary.get("total", 0)) > 0:
            break
        time.sleep(0.2)
    worktree_observer.stop("sum")
    assert summary is not None
    assert int(summary.get("total", 0)) >= 1


def test_stop_all_returns_slugs(tmp_path: Path) -> None:
    worktree_observer.start("a", tmp_path)
    worktree_observer.start("b", tmp_path)
    stopped = worktree_observer.stop_all()
    assert set(stopped) == {"a", "b"}
    assert worktree_observer.active_slugs() == []
