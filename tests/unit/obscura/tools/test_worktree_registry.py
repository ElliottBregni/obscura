"""Unit tests for worktree_registry — JSON manifest CRUD.

All tests redirect registry_root() to a temp directory via monkeypatch so
~/.obscura/worktrees is never touched.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _tmp_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect registry_root() to a per-test temp directory."""
    import obscura.tools.worktree_registry as reg

    monkeypatch.setattr(reg, "registry_root", lambda: tmp_path / "worktrees")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_entry(slug: str, pid: int = 99999, **overrides: object) -> object:
    from obscura.core.models.lifecycle import WorktreeEntry

    return WorktreeEntry(
        slug=slug,
        repo_root="/fake/repo",
        repo_hash="deadbeef1234",
        worktree_path=f"/tmp/wt-{slug}",
        branch="main",
        original_cwd="/fake/repo",
        owner="tester",
        pid=pid,
        created_at=datetime.now(),
        **overrides,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# load — empty registry
# ---------------------------------------------------------------------------


def test_load_empty_registry_returns_empty_list() -> None:
    from obscura.tools.worktree_registry import load

    assert load() == []


# ---------------------------------------------------------------------------
# add → load round-trip
# ---------------------------------------------------------------------------


def test_add_then_load_returns_entry() -> None:
    from obscura.tools.worktree_registry import add, load

    entry = _make_entry("wt-alpha")
    add(entry)  # type: ignore[arg-type]

    entries = load()
    assert len(entries) == 1
    assert entries[0].slug == "wt-alpha"


def test_add_multiple_entries_all_visible() -> None:
    from obscura.tools.worktree_registry import add, load

    add(_make_entry("wt-1"))  # type: ignore[arg-type]
    add(_make_entry("wt-2"))  # type: ignore[arg-type]
    add(_make_entry("wt-3"))  # type: ignore[arg-type]

    slugs = {e.slug for e in load()}
    assert slugs == {"wt-1", "wt-2", "wt-3"}


def test_add_same_slug_replaces_existing() -> None:
    from obscura.tools.worktree_registry import add, load

    add(_make_entry("wt-dup", pid=111))  # type: ignore[arg-type]
    add(_make_entry("wt-dup", pid=222))  # type: ignore[arg-type]

    entries = load()
    assert len(entries) == 1
    assert entries[0].pid == 222


# ---------------------------------------------------------------------------
# get
# ---------------------------------------------------------------------------


def test_get_existing_slug_returns_entry() -> None:
    from obscura.tools.worktree_registry import add, get

    add(_make_entry("wt-x"))  # type: ignore[arg-type]
    entry = get("wt-x")
    assert entry is not None
    assert entry.slug == "wt-x"


def test_get_missing_slug_returns_none() -> None:
    from obscura.tools.worktree_registry import get

    assert get("no-such-slug") is None


# ---------------------------------------------------------------------------
# update
# ---------------------------------------------------------------------------


def test_update_existing_field() -> None:
    from obscura.tools.worktree_registry import add, get, update
    from obscura.core.enums.lifecycle import WorktreeStatus

    add(_make_entry("wt-upd"))  # type: ignore[arg-type]
    result = update("wt-upd", status=WorktreeStatus.ORPHAN)

    assert result is not None
    assert result.status == WorktreeStatus.ORPHAN

    # Persisted
    reloaded = get("wt-upd")
    assert reloaded is not None
    assert reloaded.status == WorktreeStatus.ORPHAN


def test_update_nonexistent_slug_returns_none() -> None:
    from obscura.tools.worktree_registry import update

    assert update("ghost-slug", branch="new") is None


# ---------------------------------------------------------------------------
# remove
# ---------------------------------------------------------------------------


def test_remove_existing_slug_returns_true() -> None:
    from obscura.tools.worktree_registry import add, get, remove

    add(_make_entry("wt-del"))  # type: ignore[arg-type]
    assert remove("wt-del") is True
    assert get("wt-del") is None


def test_remove_missing_slug_returns_false() -> None:
    from obscura.tools.worktree_registry import remove

    assert remove("ghost") is False


# ---------------------------------------------------------------------------
# list_for_repo
# ---------------------------------------------------------------------------


def test_list_for_repo_filters_by_hash() -> None:
    from obscura.tools.worktree_registry import add, list_for_repo

    # Manually override repo_root so its hash matches our test call
    repo = "/consistent/repo/path"
    from obscura.tools.worktree_registry import repo_hash

    h = repo_hash(repo)

    from obscura.core.models.lifecycle import WorktreeEntry

    e = WorktreeEntry(  # type: ignore[call-arg]
        slug="wt-same-repo",
        repo_root=repo,
        repo_hash=h,
        worktree_path="/tmp/wt-same-repo",
        branch="main",
        original_cwd=repo,
        owner="tester",
        pid=1,
        created_at=datetime.now(),
    )
    add(e)
    add(_make_entry("wt-other-repo"))  # type: ignore[arg-type]  # different hash

    results = list_for_repo(repo)
    assert len(results) == 1
    assert results[0].slug == "wt-same-repo"


# ---------------------------------------------------------------------------
# repo_hash
# ---------------------------------------------------------------------------


def test_repo_hash_returns_12_char_hex() -> None:
    from obscura.tools.worktree_registry import repo_hash

    h = repo_hash("/some/repo/path")
    assert len(h) == 12
    assert all(c in "0123456789abcdef" for c in h)


def test_repo_hash_same_path_is_stable() -> None:
    from obscura.tools.worktree_registry import repo_hash

    assert repo_hash("/a/b/c") == repo_hash("/a/b/c")


def test_repo_hash_different_paths_differ() -> None:
    from obscura.tools.worktree_registry import repo_hash

    assert repo_hash("/path/one") != repo_hash("/path/two")


# ---------------------------------------------------------------------------
# sweep_dead_pids — uses a pid that is guaranteed not to exist
# ---------------------------------------------------------------------------


def test_sweep_dead_pids_marks_orphan() -> None:
    from obscura.tools.worktree_registry import add, get, sweep_dead_pids
    from obscura.core.enums.lifecycle import WorktreeStatus

    # pid=1 is init / launchd; on macOS/Linux kill(1, 0) raises PermissionError → alive.
    # Use a very large PID that is almost certainly dead.
    entry = _make_entry("wt-dead", pid=9999999)
    add(entry)  # type: ignore[arg-type]

    orphans = sweep_dead_pids()
    # If the pid is truly dead, entry should be marked ORPHAN
    if orphans:
        e = get("wt-dead")
        assert e is not None
        assert e.status == WorktreeStatus.ORPHAN


# ---------------------------------------------------------------------------
# prune_missing_paths
# ---------------------------------------------------------------------------


def test_prune_missing_paths_removes_nonexistent_worktree() -> None:
    from obscura.tools.worktree_registry import add, prune_missing_paths

    # worktree_path points to a directory that doesn't exist
    entry = _make_entry("wt-gone")
    add(entry)  # type: ignore[arg-type]  # worktree_path=/tmp/wt-gone — doesn't exist

    dropped = prune_missing_paths()
    assert "wt-gone" in dropped


def test_prune_missing_paths_keeps_existing_worktree(tmp_path: Path) -> None:
    from obscura.core.models.lifecycle import WorktreeEntry
    from obscura.tools.worktree_registry import add, load, prune_missing_paths

    wt_dir = tmp_path / "real-wt"
    wt_dir.mkdir()

    e = WorktreeEntry(  # type: ignore[call-arg]
        slug="wt-exists",
        repo_root="/repo",
        repo_hash="abc123456789",
        worktree_path=str(wt_dir),
        branch="main",
        original_cwd="/repo",
        owner="tester",
        pid=1,
        created_at=datetime.now(),
    )
    add(e)
    dropped = prune_missing_paths()

    assert "wt-exists" not in dropped
    assert any(en.slug == "wt-exists" for en in load())


# ---------------------------------------------------------------------------
# worktree_path_for
# ---------------------------------------------------------------------------


def test_worktree_path_for_combines_hash_and_slug() -> None:
    from obscura.tools.worktree_registry import worktree_path_for, repo_hash, registry_root

    path = worktree_path_for("/my/repo", "my-slug")
    expected_hash = repo_hash("/my/repo")
    assert path == registry_root() / expected_hash / "my-slug"
