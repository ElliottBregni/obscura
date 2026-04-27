"""Tests for the /worktree CLI command."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

from obscura.cli.commands import COMMANDS, COMPLETIONS, cmd_worktree
from obscura.tools import worktree_registry


@pytest.fixture(autouse=True)
def isolate_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    fake_home = tmp_path / "home"
    fake_home.mkdir()

    def _home() -> Path:
        return fake_home

    monkeypatch.setattr(Path, "home", staticmethod(_home))
    return fake_home


def _ctx() -> Any:
    return MagicMock()


def _entry(
    slug: str, pid: int | None = None, status: str = "active"
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
        status=status,
    )


def test_registered_in_commands_map() -> None:
    assert COMMANDS["worktree"] is cmd_worktree
    assert COMPLETIONS["worktree"] == ["list", "status", "sweep", "cleanup"]


@pytest.mark.asyncio
async def test_list_all_runs_without_error() -> None:
    worktree_registry.add(_entry("alpha"))
    worktree_registry.add(_entry("beta"))
    assert await cmd_worktree("list", _ctx()) is None


@pytest.mark.asyncio
async def test_status_known_slug() -> None:
    worktree_registry.add(_entry("alpha"))
    assert await cmd_worktree("status alpha", _ctx()) is None


@pytest.mark.asyncio
async def test_status_unknown_slug() -> None:
    assert await cmd_worktree("status does-not-exist", _ctx()) is None


@pytest.mark.asyncio
async def test_status_requires_slug() -> None:
    assert await cmd_worktree("status", _ctx()) is None


@pytest.mark.asyncio
async def test_sweep_marks_dead_pid_orphan() -> None:
    worktree_registry.add(_entry("dead", pid=2**31 - 1))
    await cmd_worktree("sweep", _ctx())
    got = worktree_registry.get("dead")
    assert got is not None
    assert got.status == "orphan"


@pytest.mark.asyncio
async def test_cleanup_prunes_missing_and_sweeps() -> None:
    # dead-PID entry becomes orphan
    worktree_registry.add(_entry("dead", pid=2**31 - 1))
    # Path never created -> prune after sweep marks it orphan? Pruning keeps "kept".
    # Here "dead" is "active" -> after sweep becomes "orphan" -> prune drops
    # because its path doesn't exist.
    await cmd_worktree("cleanup", _ctx())
    assert worktree_registry.get("dead") is None


@pytest.mark.asyncio
async def test_unknown_subcommand_prints_usage() -> None:
    assert await cmd_worktree("bogus", _ctx()) is None


@pytest.mark.asyncio
async def test_bare_invocation_in_non_repo_prints_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Point cwd at a non-git tmp dir so `git rev-parse` fails.
    non_repo = tmp_path / "not-a-repo"
    non_repo.mkdir()
    monkeypatch.chdir(non_repo)
    assert await cmd_worktree("", _ctx()) is None


@pytest.mark.asyncio
async def test_help_mentions_worktree() -> None:
    # Quick sanity: /help string block includes /worktree.
    from obscura.cli.commands import cmd_help

    captured: list[str] = []

    import obscura.cli.commands as cmds

    real_console = cast("Any", cmds.console)
    original_print = real_console.print

    def capture(msg: Any = "", *_a: Any, **_kw: Any) -> None:
        captured.append(str(msg))

    real_console.print = capture
    try:
        await cmd_help("", _ctx())
    finally:
        real_console.print = original_print
    assert any("/worktree" in line for line in captured)
