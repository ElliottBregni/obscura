"""Unit tests for obscura.tools.worktree — enter/exit worktree tools.

Mock strategy:
  - Patch `_git` (the internal git subprocess coroutine) at the module level
  - Patch `worktree_registry` functions (get, add, remove, update, load)
  - Patch `worktree_observer` functions (start, stop, summary)
  - Patch `os.getcwd` / `os.chdir` to avoid filesystem side-effects
  - Patch `register_cleanup` to avoid real cleanup hooks
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import obscura.tools.worktree as _wt_mod
from obscura.tools.worktree import enter_worktree, exit_worktree

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# enter_worktree
# ---------------------------------------------------------------------------


async def test_enter_worktree_invalid_name_returns_error() -> None:
    result = json.loads(await enter_worktree(name="has spaces!"))

    assert result["ok"] is False
    assert result["error"] == "invalid_name"


async def test_enter_worktree_slug_in_use_returns_error() -> None:
    with patch.object(_wt_mod.worktree_registry, "get", return_value=MagicMock()):
        result = json.loads(await enter_worktree(name="existing-slug"))

    assert result["ok"] is False
    assert result["error"] == "slug_in_use"


async def test_enter_worktree_not_in_git_repo_returns_error() -> None:
    with (
        patch.object(_wt_mod.worktree_registry, "get", return_value=None),
        patch.object(
            _wt_mod,
            "_main_repo_root",
            new=AsyncMock(return_value=(128, "", "not a git repo")),
        ),
    ):
        result = json.loads(await enter_worktree(name="my-wt"))

    assert result["ok"] is False
    assert result["error"] == "not_a_git_repo"


async def test_enter_worktree_git_worktree_add_fails_returns_error(
    tmp_path: Path,
) -> None:
    mock_entry = MagicMock()
    mock_entry.slug = "my-wt"

    with (
        patch.object(_wt_mod.worktree_registry, "get", return_value=None),
        patch.object(
            _wt_mod,
            "_main_repo_root",
            new=AsyncMock(return_value=(0, str(tmp_path), "")),
        ),
        patch.object(
            _wt_mod.worktree_registry,
            "worktree_path_for",
            return_value=tmp_path / "worktrees" / "my-wt",
        ),
        patch.object(
            _wt_mod,
            "_git",
            new=AsyncMock(return_value=(1, "", "branch already exists")),
        ),
        patch.object(_wt_mod.worktree_registry, "repo_hash", return_value="abc123"),
    ):
        result = json.loads(await enter_worktree(name="my-wt"))

    assert result["ok"] is False
    assert result["error"] == "worktree_create_failed"


async def test_enter_worktree_success(tmp_path: Path) -> None:
    wt_path = tmp_path / "worktrees" / "my-wt"
    wt_path.mkdir(parents=True)

    with (
        patch.object(_wt_mod.worktree_registry, "get", return_value=None),
        patch.object(
            _wt_mod,
            "_main_repo_root",
            new=AsyncMock(return_value=(0, str(tmp_path), "")),
        ),
        patch.object(
            _wt_mod.worktree_registry,
            "worktree_path_for",
            return_value=wt_path,
        ),
        patch.object(_wt_mod, "_git", new=AsyncMock(return_value=(0, "", ""))),
        patch.object(_wt_mod.worktree_registry, "repo_hash", return_value="abc123"),
        patch.object(_wt_mod.worktree_registry, "add"),
        patch.object(_wt_mod.worktree_observer, "start", return_value=True),
        patch.object(_wt_mod, "_ensure_cleanup_registered"),
        patch("os.getcwd", return_value=str(tmp_path)),
        patch("os.chdir"),
    ):
        result = json.loads(await enter_worktree(name="my-wt"))

    assert result["ok"] is True
    assert result["slug"] == "my-wt"
    assert "worktree_path" in result


# ---------------------------------------------------------------------------
# exit_worktree
# ---------------------------------------------------------------------------


async def test_exit_worktree_no_active_worktree_returns_error() -> None:
    with (
        patch.object(_wt_mod.worktree_registry, "load", return_value=[]),
        patch.object(_wt_mod.worktree_registry, "get", return_value=None),
    ):
        result = json.loads(await exit_worktree(action="keep"))

    assert result["ok"] is False
    assert result["error"] == "not_in_worktree"


async def test_exit_worktree_unknown_slug_returns_error() -> None:
    with patch.object(_wt_mod.worktree_registry, "get", return_value=None):
        result = json.loads(await exit_worktree(action="keep", name="missing"))

    assert result["ok"] is False
    assert result["error"] == "unknown_worktree"


async def test_exit_worktree_keep_action_succeeds(tmp_path: Path) -> None:
    entry = MagicMock()
    entry.worktree_path = str(tmp_path / "wt")
    entry.branch = "worktree/my-wt"
    entry.original_cwd = str(tmp_path)
    entry.repo_root = str(tmp_path)

    with (
        patch.object(_wt_mod.worktree_registry, "get", return_value=entry),
        patch.object(_wt_mod.worktree_observer, "stop"),
        patch.object(_wt_mod.worktree_observer, "summary", return_value=None),
        patch.object(_wt_mod.worktree_registry, "update"),
        patch("os.getcwd", return_value=str(tmp_path)),
    ):
        result = json.loads(await exit_worktree(action="keep", name="my-wt"))

    assert result["ok"] is True
    assert result["action"] == "keep"


async def test_exit_worktree_remove_uncommitted_changes_no_force_returns_error(
    tmp_path: Path,
) -> None:
    entry = MagicMock()
    entry.worktree_path = str(tmp_path / "wt")
    entry.branch = "worktree/my-wt"
    entry.original_cwd = str(tmp_path)
    entry.repo_root = str(tmp_path)

    # Mock git status returning uncommitted files
    with (
        patch.object(_wt_mod.worktree_registry, "get", return_value=entry),
        patch.object(_wt_mod.worktree_observer, "stop"),
        patch.object(_wt_mod.worktree_observer, "summary", return_value=None),
        patch.object(
            _wt_mod,
            "_git",
            new=AsyncMock(return_value=(0, " M file.py\n", "")),
        ),
        patch("os.getcwd", return_value=str(tmp_path)),
    ):
        result = json.loads(
            await exit_worktree(action="remove", name="my-wt", discard_changes=False)
        )

    assert result["ok"] is False
    assert result["error"] == "uncommitted_changes"


async def test_exit_worktree_remove_with_discard_changes_succeeds(
    tmp_path: Path,
) -> None:
    entry = MagicMock()
    entry.worktree_path = str(tmp_path / "wt")
    entry.branch = "worktree/my-wt"
    entry.original_cwd = str(tmp_path)
    entry.repo_root = str(tmp_path)

    with (
        patch.object(_wt_mod.worktree_registry, "get", return_value=entry),
        patch.object(_wt_mod.worktree_observer, "stop"),
        patch.object(_wt_mod.worktree_observer, "summary", return_value=None),
        patch.object(
            _wt_mod,
            "_git",
            new=AsyncMock(return_value=(0, " M file.py\n", "")),
        ),
        patch.object(_wt_mod.worktree_registry, "remove"),
        patch("os.getcwd", return_value=str(tmp_path)),
        patch("os.chdir"),
    ):
        result = json.loads(
            await exit_worktree(action="remove", name="my-wt", discard_changes=True)
        )

    assert result["ok"] is True
    assert result["action"] == "remove"


async def test_exit_worktree_clean_remove_succeeds(tmp_path: Path) -> None:
    entry = MagicMock()
    entry.worktree_path = str(tmp_path / "wt")
    entry.branch = "worktree/my-wt"
    entry.original_cwd = str(tmp_path)
    entry.repo_root = str(tmp_path)

    with (
        patch.object(_wt_mod.worktree_registry, "get", return_value=entry),
        patch.object(_wt_mod.worktree_observer, "stop"),
        patch.object(
            _wt_mod.worktree_observer, "summary", return_value={"files_changed": 0}
        ),
        patch.object(
            _wt_mod,
            "_git",
            new=AsyncMock(return_value=(0, "", "")),  # no uncommitted changes
        ),
        patch.object(_wt_mod.worktree_registry, "remove"),
        patch("os.getcwd", return_value=str(tmp_path)),
        patch("os.chdir"),
    ):
        result = json.loads(
            await exit_worktree(action="remove", name="my-wt", discard_changes=False)
        )

    assert result["ok"] is True
    assert "Removed" in result["message"]
