"""Tests for obscura.tools.worktree_registry."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from obscura.tools import worktree_registry


@pytest.fixture(autouse=True)
def isolate_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the registry root at a tmp directory for every test."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    def _home() -> Path:
        return fake_home

    monkeypatch.setattr(Path, "home", staticmethod(_home))
    return fake_home


def _make_entry(
    slug: str = "wt-a", pid: int | None = None
) -> worktree_registry.WorktreeEntry:
    repo = "/tmp/some/repo"
    return worktree_registry.WorktreeEntry(
        slug=slug,
        repo_root=repo,
        repo_hash=worktree_registry.repo_hash(repo),
        worktree_path=str(worktree_registry.worktree_path_for(repo, slug)),
        branch=f"worktree/{slug}",
        original_cwd="/tmp",
        owner="tool",
        pid=pid if pid is not None else os.getpid(),
        created_at=time.time(),
    )


def test_registry_root_under_home() -> None:
    root = worktree_registry.registry_root()
    assert root.name == "worktrees"
    assert root.parent.name == ".obscura"


def test_repo_hash_is_stable() -> None:
    a = worktree_registry.repo_hash("/tmp/x")
    b = worktree_registry.repo_hash("/tmp/x")
    assert a == b
    assert len(a) == 12


def test_repo_hash_differs_by_path() -> None:
    assert worktree_registry.repo_hash("/tmp/a") != worktree_registry.repo_hash(
        "/tmp/b"
    )


def test_worktree_path_under_registry_root() -> None:
    path = worktree_registry.worktree_path_for("/tmp/repo", "slug-1")
    assert worktree_registry.registry_root() in path.parents
    assert path.name == "slug-1"


def test_add_and_get() -> None:
    entry = _make_entry()
    worktree_registry.add(entry)
    loaded = worktree_registry.get("wt-a")
    assert loaded is not None
    assert loaded.slug == "wt-a"
    assert loaded.branch == "worktree/wt-a"


def test_add_replaces_by_slug() -> None:
    worktree_registry.add(_make_entry("dup"))
    worktree_registry.add(_make_entry("dup"))
    assert len(worktree_registry.load()) == 1


def test_update_existing() -> None:
    worktree_registry.add(_make_entry("wt-up"))
    result = worktree_registry.update("wt-up", status="kept")
    assert result is not None
    assert result.status == "kept"
    assert worktree_registry.get("wt-up") is not None
    got = worktree_registry.get("wt-up")
    assert got is not None
    assert got.status == "kept"


def test_update_missing_returns_none() -> None:
    assert worktree_registry.update("nope", status="orphan") is None


def test_remove() -> None:
    worktree_registry.add(_make_entry("wt-rm"))
    assert worktree_registry.remove("wt-rm") is True
    assert worktree_registry.remove("wt-rm") is False
    assert worktree_registry.get("wt-rm") is None


def test_list_for_repo_filters_by_hash() -> None:
    worktree_registry.add(_make_entry("a"))
    # Different repo
    repo_b = "/tmp/other/repo"
    worktree_registry.add(
        worktree_registry.WorktreeEntry(
            slug="b",
            repo_root=repo_b,
            repo_hash=worktree_registry.repo_hash(repo_b),
            worktree_path=str(worktree_registry.worktree_path_for(repo_b, "b")),
            branch="worktree/b",
            original_cwd="/tmp",
            owner="tool",
            pid=os.getpid(),
            created_at=time.time(),
        ),
    )
    a_only = worktree_registry.list_for_repo("/tmp/some/repo")
    assert [e.slug for e in a_only] == ["a"]


def test_sweep_dead_pids_marks_orphans() -> None:
    # Use a PID that will never exist.
    worktree_registry.add(_make_entry("dead", pid=2**31 - 1))
    orphans = worktree_registry.sweep_dead_pids()
    assert any(e.slug == "dead" for e in orphans)
    got = worktree_registry.get("dead")
    assert got is not None
    assert got.status == "orphan"


def test_sweep_leaves_live_pids_alone() -> None:
    worktree_registry.add(_make_entry("live"))
    worktree_registry.sweep_dead_pids()
    got = worktree_registry.get("live")
    assert got is not None
    assert got.status == "active"


def test_prune_missing_paths() -> None:
    entry = _make_entry("gone")
    worktree_registry.add(entry)
    # Path was never created on disk.
    dropped = worktree_registry.prune_missing_paths()
    assert "gone" in dropped
    assert worktree_registry.get("gone") is None


def test_prune_keeps_kept_entries_even_without_path() -> None:
    entry = _make_entry("keep-me")
    worktree_registry.add(entry)
    worktree_registry.update("keep-me", status="kept")
    dropped = worktree_registry.prune_missing_paths()
    assert "keep-me" not in dropped


def test_cleanup_orphan_dirs_removes_unknown_checkouts() -> None:
    # Build a fake orphan worktree directory on disk (not in registry).
    repo = "/tmp/some/repo"
    orphan = worktree_registry.worktree_path_for(repo, "ghost")
    orphan.mkdir(parents=True)
    (orphan / ".git").write_text("gitdir: /nowhere\n")

    removed = worktree_registry.cleanup_orphan_dirs()
    assert removed == 1
    assert not orphan.exists()


def test_cleanup_orphan_dirs_ignores_known() -> None:
    entry = _make_entry("known")
    Path(entry.worktree_path).mkdir(parents=True)
    (Path(entry.worktree_path) / ".git").write_text("gitdir: /somewhere\n")
    worktree_registry.add(entry)

    removed = worktree_registry.cleanup_orphan_dirs()
    assert removed == 0
    assert Path(entry.worktree_path).exists()
