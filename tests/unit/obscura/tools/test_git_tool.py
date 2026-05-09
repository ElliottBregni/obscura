"""Unit tests for the unified Git tool dispatcher.

All subprocess calls are intercepted via AsyncMock so no real git
operations hit the filesystem.
"""
# pyright: reportPrivateUsage=false

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import obscura.tools.system._git as _git_mod
from obscura.tools.system._git import Git

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_proc(
    stdout: bytes = b"",
    stderr: bytes = b"",
    returncode: int = 0,
) -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.wait = AsyncMock()
    proc.kill = MagicMock()
    return proc


# ---------------------------------------------------------------------------
# git_subprocess (internal helper)
# ---------------------------------------------------------------------------


async def test_git_subprocess_success_returns_ok() -> None:
    proc = _fake_proc(stdout=b"on branch main\n")
    with (
        patch.object(_git_mod.shutil, "which", return_value="/usr/bin/git"),
        patch.object(
            _git_mod.asyncio,
            "create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ),
    ):
        result = await Git.git_subprocess(["status", "--short", "--branch"])

    assert result["ok"] is True
    assert "main" in result["stdout"]
    assert result["exit_code"] == 0


async def test_git_subprocess_git_not_found_returns_error() -> None:
    with patch.object(_git_mod.shutil, "which", return_value=None):
        result = await Git.git_subprocess(["status"])

    assert result["ok"] is False
    assert result["error"] == "git_not_found"


async def test_git_subprocess_nonzero_exit_returns_ok_false() -> None:
    proc = _fake_proc(stderr=b"fatal: not a git repo\n", returncode=128)
    with (
        patch.object(_git_mod.shutil, "which", return_value="/usr/bin/git"),
        patch.object(
            _git_mod.asyncio,
            "create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ),
    ):
        result = await Git.git_subprocess(["status"])

    assert result["ok"] is False
    assert result["exit_code"] == 128


# ---------------------------------------------------------------------------
# action="status"
# ---------------------------------------------------------------------------


async def test_git_status_short_format() -> None:
    stdout = b" M src/main.py\n## main...origin/main\n"
    proc = _fake_proc(stdout=stdout)
    with (
        patch.object(_git_mod.shutil, "which", return_value="/usr/bin/git"),
        patch.object(
            _git_mod.asyncio, "create_subprocess_exec", new=AsyncMock(return_value=proc)
        ) as mock_exec,
    ):
        result = json.loads(await Git.git(action="status"))

    assert result["ok"] is True
    assert "main.py" in result["stdout"]
    # --short and --branch flags should have been passed
    call_args = mock_exec.call_args[0]
    assert "--short" in call_args
    assert "--branch" in call_args


async def test_git_status_long_format() -> None:
    proc = _fake_proc(stdout=b"On branch main\nnothing to commit\n")
    with (
        patch.object(_git_mod.shutil, "which", return_value="/usr/bin/git"),
        patch.object(
            _git_mod.asyncio, "create_subprocess_exec", new=AsyncMock(return_value=proc)
        ) as mock_exec,
    ):
        await Git.git(action="status", short=False)

    call_args = mock_exec.call_args[0]
    assert "--short" not in call_args


# ---------------------------------------------------------------------------
# action="diff"
# ---------------------------------------------------------------------------


async def test_git_diff_basic() -> None:
    diff_output = b"diff --git a/f.py b/f.py\n-old\n+new\n"
    proc = _fake_proc(stdout=diff_output)
    with (
        patch.object(_git_mod.shutil, "which", return_value="/usr/bin/git"),
        patch.object(
            _git_mod.asyncio, "create_subprocess_exec", new=AsyncMock(return_value=proc)
        ),
    ):
        result = json.loads(await Git.git(action="diff"))

    assert result["ok"] is True
    assert "diff" in result["stdout"]


async def test_git_diff_staged_adds_cached_flag() -> None:
    proc = _fake_proc(stdout=b"staged diff\n")
    with (
        patch.object(_git_mod.shutil, "which", return_value="/usr/bin/git"),
        patch.object(
            _git_mod.asyncio, "create_subprocess_exec", new=AsyncMock(return_value=proc)
        ) as mock_exec,
    ):
        await Git.git(action="diff", staged=True)

    call_args = mock_exec.call_args[0]
    assert "--cached" in call_args


async def test_git_diff_large_output_is_truncated() -> None:
    big_diff = b"+" + b"x" * 200_000 + b"\n"
    proc = _fake_proc(stdout=big_diff)
    with (
        patch.object(_git_mod.shutil, "which", return_value="/usr/bin/git"),
        patch.object(
            _git_mod.asyncio, "create_subprocess_exec", new=AsyncMock(return_value=proc)
        ),
    ):
        result = json.loads(await Git.git(action="diff"))

    assert result["ok"] is True
    assert result.get("truncated") is True
    assert len(result["stdout"]) < len(big_diff)


# ---------------------------------------------------------------------------
# action="log"
# ---------------------------------------------------------------------------


async def test_git_log_oneline() -> None:
    log_output = b"abc1234 Fix bug\ndef5678 Add feature\n"
    proc = _fake_proc(stdout=log_output)
    with (
        patch.object(_git_mod.shutil, "which", return_value="/usr/bin/git"),
        patch.object(
            _git_mod.asyncio, "create_subprocess_exec", new=AsyncMock(return_value=proc)
        ) as mock_exec,
    ):
        result = json.loads(await Git.git(action="log", max_count=5))

    assert result["ok"] is True
    assert "Fix bug" in result["stdout"]
    call_args = mock_exec.call_args[0]
    assert "--oneline" in call_args
    assert "-5" in call_args


async def test_git_log_max_count_clamped_to_100() -> None:
    """max_count is clamped to [1, 100]."""
    proc = _fake_proc(stdout=b"commit 1\n")
    with (
        patch.object(_git_mod.shutil, "which", return_value="/usr/bin/git"),
        patch.object(
            _git_mod.asyncio, "create_subprocess_exec", new=AsyncMock(return_value=proc)
        ) as mock_exec,
    ):
        await Git.git(action="log", max_count=999)

    call_args = mock_exec.call_args[0]
    assert "-100" in call_args  # clamped to max 100


# ---------------------------------------------------------------------------
# action="commit"
# ---------------------------------------------------------------------------


async def test_git_commit_success() -> None:
    # Two subprocess calls: git add, then git commit
    add_proc = _fake_proc(stdout=b"", returncode=0)
    commit_proc = _fake_proc(
        stdout=b"[main abc1234] My commit\n 1 file changed\n", returncode=0
    )
    call_count = 0

    async def _side_effect(*args: object, **kwargs: object) -> MagicMock:
        nonlocal call_count
        call_count += 1
        return add_proc if call_count == 1 else commit_proc

    with (
        patch.object(_git_mod.shutil, "which", return_value="/usr/bin/git"),
        patch.object(
            _git_mod.asyncio, "create_subprocess_exec", side_effect=_side_effect
        ),
    ):
        result = json.loads(
            await Git.git(action="commit", message="My commit", files=["src/main.py"])
        )

    assert result["ok"] is True
    assert "My commit" in result["stdout"]


async def test_git_commit_empty_message_returns_error() -> None:
    result = json.loads(await Git.git(action="commit", message="   "))

    assert result["ok"] is False
    assert "empty_commit_message" in result.get("error", "")


# ---------------------------------------------------------------------------
# action="branch"
# ---------------------------------------------------------------------------


async def test_git_branch_list() -> None:
    proc = _fake_proc(stdout=b"* main\n  feature/x\n")
    with (
        patch.object(_git_mod.shutil, "which", return_value="/usr/bin/git"),
        patch.object(
            _git_mod.asyncio, "create_subprocess_exec", new=AsyncMock(return_value=proc)
        ) as mock_exec,
    ):
        result = json.loads(await Git.git(action="branch", sub_action="list"))

    assert result["ok"] is True
    assert "main" in result["stdout"]
    call_args = mock_exec.call_args[0]
    assert "branch" in call_args


async def test_git_branch_create_no_ref_returns_error() -> None:
    result = json.loads(await Git.git(action="branch", sub_action="create", ref=""))

    assert result["ok"] is False
    assert "branch_name_required" in result.get("error", "")


async def test_git_branch_invalid_sub_action_returns_error() -> None:
    result = json.loads(await Git.git(action="branch", sub_action="explode"))

    assert result["ok"] is False
    assert "invalid_sub_action" in result.get("error", "")
