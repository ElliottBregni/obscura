"""Integration tests for enter_worktree / exit_worktree tools.

These tests exercise the actual tool functions against a real git repo in
``tmp_path``, so they depend on ``git`` being on PATH.  They verify that
checkouts land under ``~/.obscura/worktrees/{repo_hash}/{slug}/`` and that
the registry + observer are wired up.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

import obscura.tools.worktree_observer as worktree_observer
from obscura.tools import worktree_registry
from obscura.tools.worktree import enter_worktree, exit_worktree


@pytest.fixture
def temp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    fake_home = tmp_path / "home"
    fake_home.mkdir()

    def _home() -> Path:
        return fake_home

    monkeypatch.setattr(Path, "home", staticmethod(_home))
    yield fake_home
    worktree_observer.stop_all()


@pytest.fixture
def git_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "README.md").write_text("hello")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    monkeypatch.chdir(repo)
    return repo


@pytest.mark.asyncio
async def test_enter_worktree_uses_home_path(
    temp_home: Path,
    git_repo: Path,
) -> None:
    result = json.loads(await enter_worktree(name="alpha"))
    try:
        assert result["ok"] is True
        assert result["slug"] == "alpha"
        wt_path = Path(result["worktree_path"])
        assert wt_path.is_dir()
        # Must be under ~/.obscura/worktrees/{hash}/{slug}
        assert temp_home / ".obscura" / "worktrees" in wt_path.parents
        # Registry knows about it
        entry = worktree_registry.get("alpha")
        assert entry is not None
        assert entry.owner == "tool"
        assert entry.pid == os.getpid()
        # Observer is running
        assert "alpha" in worktree_observer.active_slugs()
    finally:
        await exit_worktree(action="remove", name="alpha", discard_changes=True)


@pytest.mark.asyncio
async def test_enter_rejects_invalid_name(temp_home: Path, git_repo: Path) -> None:
    result = json.loads(await enter_worktree(name="bad name!"))
    assert result["ok"] is False
    assert result["error"] == "invalid_name"


@pytest.mark.asyncio
async def test_enter_rejects_duplicate_slug(temp_home: Path, git_repo: Path) -> None:
    first = json.loads(await enter_worktree(name="dup"))
    assert first["ok"] is True
    try:
        second = json.loads(await enter_worktree(name="dup"))
        assert second["ok"] is False
        assert second["error"] == "slug_in_use"
    finally:
        await exit_worktree(action="remove", name="dup", discard_changes=True)


@pytest.mark.asyncio
async def test_exit_removes_registry_entry_and_observer(
    temp_home: Path,
    git_repo: Path,
) -> None:
    await enter_worktree(name="beta")
    result = json.loads(
        await exit_worktree(action="remove", name="beta", discard_changes=True)
    )
    assert result["ok"] is True
    assert worktree_registry.get("beta") is None
    assert "beta" not in worktree_observer.active_slugs()


@pytest.mark.asyncio
async def test_exit_keep_marks_status_kept(temp_home: Path, git_repo: Path) -> None:
    await enter_worktree(name="gamma")
    entry = worktree_registry.get("gamma")
    assert entry is not None
    try:
        result = json.loads(await exit_worktree(action="keep", name="gamma"))
        assert result["ok"] is True
        kept = worktree_registry.get("gamma")
        assert kept is not None
        assert kept.status == "kept"
        # Observer should also be stopped on exit.
        assert "gamma" not in worktree_observer.active_slugs()
    finally:
        subprocess.run(
            [
                "git",
                "worktree",
                "remove",
                "--force",
                Path(entry.worktree_path).as_posix(),
            ],
            cwd=git_repo,
            check=False,
        )
        subprocess.run(["git", "branch", "-D", entry.branch], cwd=git_repo, check=False)
        worktree_registry.remove("gamma")


@pytest.mark.asyncio
async def test_exit_without_name_targets_most_recent(
    temp_home: Path,
    git_repo: Path,
) -> None:
    await enter_worktree(name="first")
    await enter_worktree(name="second")
    try:
        result = json.loads(await exit_worktree(action="remove", discard_changes=True))
        assert result["ok"] is True
        assert result["slug"] == "second"
    finally:
        # first is still around.
        await exit_worktree(action="remove", name="first", discard_changes=True)


@pytest.mark.asyncio
async def test_concurrent_worktrees_do_not_clash(
    temp_home: Path,
    git_repo: Path,
) -> None:
    a = json.loads(await enter_worktree(name="wt-a"))
    b = json.loads(await enter_worktree(name="wt-b"))
    try:
        assert a["ok"] and b["ok"]
        assert a["worktree_path"] != b["worktree_path"]
        assert {"wt-a", "wt-b"} <= set(worktree_observer.active_slugs())
    finally:
        await exit_worktree(action="remove", name="wt-a", discard_changes=True)
        await exit_worktree(action="remove", name="wt-b", discard_changes=True)
