"""obscura.tools.worktree — Git worktree isolation tools.

Provides ``enter_worktree`` and ``exit_worktree`` tools that let agents
work in isolated git worktrees, preventing changes from polluting the
main working tree until explicitly merged.

Pattern borrowed from claude-code's ``EnterWorktreeTool``/``ExitWorktreeTool``.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from obscura.core.tools import tool

if TYPE_CHECKING:
    from obscura.core.types import ToolSpec

# Module-level state tracking active worktree sessions.
# Maps session_key -> {"original_cwd": str, "worktree_path": str, "branch": str}
_worktree_sessions: dict[str, dict[str, str]] = {}

_SESSION_KEY = "default"  # Single-session for now; extensible later.


def _json_error(error: str, **extra: object) -> str:
    payload: dict[str, object] = {"ok": False, "error": error}
    payload.update(extra)
    return json.dumps(payload)


async def _git(
    *args: str,
    cwd: str | None = None,
) -> tuple[int, str, str]:
    """Run a git command and return (exit_code, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return (
        proc.returncode or 0,
        stdout.decode("utf-8", errors="replace").strip(),
        stderr.decode("utf-8", errors="replace").strip(),
    )


@tool(
    "enter_worktree",
    (
        "Create and enter a git worktree for isolated work. "
        "Changes in the worktree do not affect the main working tree. "
        "Use exit_worktree to return."
    ),
    {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Optional worktree name (alphanumeric, dash, dot, underscore; max 64 chars).",
            },
        },
    },
)
async def enter_worktree(name: str = "") -> str:
    # Check not already in a worktree session.
    if _SESSION_KEY in _worktree_sessions:
        return _json_error(
            "already_in_worktree",
            detail="Already in a worktree session. Exit the current one first.",
            worktree_path=_worktree_sessions[_SESSION_KEY].get("worktree_path", ""),
        )

    # Validate name.
    slug = name.strip() if name else f"obscura-wt-{int(time.time())}"
    if not re.match(r"^[a-zA-Z0-9._-]{1,64}$", slug):
        return _json_error(
            "invalid_name",
            detail="Name must be alphanumeric/dash/dot/underscore, max 64 chars.",
        )

    # Find git root.
    rc, git_root, err = await _git("rev-parse", "--show-toplevel")
    if rc != 0:
        return _json_error("not_a_git_repo", detail=err)

    # Create worktree.
    branch_name = f"worktree/{slug}"
    worktree_path = str(Path(git_root).parent / ".obscura-worktrees" / slug)

    rc, _, err = await _git(
        "worktree",
        "add",
        "-b",
        branch_name,
        worktree_path,
        cwd=git_root,
    )
    if rc != 0:
        return _json_error("worktree_create_failed", detail=err)

    # Track session state.
    original_cwd = os.getcwd()
    _worktree_sessions[_SESSION_KEY] = {
        "original_cwd": original_cwd,
        "worktree_path": worktree_path,
        "branch": branch_name,
        "git_root": git_root,
    }

    # Change to worktree directory.
    os.chdir(worktree_path)

    return json.dumps(
        {
            "ok": True,
            "worktree_path": worktree_path,
            "branch": branch_name,
            "original_cwd": original_cwd,
            "message": f"Entered worktree at {worktree_path} on branch {branch_name}",
        },
    )


@tool(
    "exit_worktree",
    (
        "Exit the current git worktree and return to the original directory. "
        "Use action='keep' to preserve the worktree, or 'remove' to delete it."
    ),
    {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["keep", "remove"],
                "description": "'keep' preserves worktree+branch; 'remove' deletes them.",
            },
            "discard_changes": {
                "type": "boolean",
                "description": "Required when removing a worktree with uncommitted changes.",
            },
        },
        "required": ["action"],
    },
)
async def exit_worktree(
    action: str = "keep",
    discard_changes: bool = False,
) -> str:
    session = _worktree_sessions.get(_SESSION_KEY)
    if session is None:
        return _json_error("not_in_worktree", detail="No active worktree session.")

    worktree_path = session["worktree_path"]
    branch = session["branch"]
    original_cwd = session["original_cwd"]
    git_root = session["git_root"]

    result: dict[str, Any] = {
        "ok": True,
        "action": action,
        "worktree_path": worktree_path,
        "branch": branch,
        "original_cwd": original_cwd,
    }

    if action == "remove":
        # Check for uncommitted changes.
        _rc, status_out, _ = await _git("status", "--porcelain", cwd=worktree_path)
        _rc2, _log_out, _ = await _git(
            "rev-list",
            "--count",
            "HEAD",
            f"^{branch}~0",
            cwd=worktree_path,
        )
        uncommitted_files = len([ln for ln in status_out.splitlines() if ln.strip()])

        if uncommitted_files > 0 and not discard_changes:
            return _json_error(
                "uncommitted_changes",
                detail=f"{uncommitted_files} uncommitted file(s). Set discard_changes=true to force remove.",
                uncommitted_files=uncommitted_files,
            )

        result["discarded_files"] = uncommitted_files

    # Return to original directory.
    os.chdir(original_cwd)

    if action == "remove":
        # Remove worktree and branch.
        await _git("worktree", "remove", "--force", worktree_path, cwd=git_root)
        await _git("branch", "-D", branch, cwd=git_root)
        result["message"] = f"Removed worktree and branch {branch}"
    else:
        result["message"] = f"Kept worktree at {worktree_path} on branch {branch}"

    # Clear session state.
    del _worktree_sessions[_SESSION_KEY]

    return json.dumps(result)


def get_worktree_tool_specs() -> list[ToolSpec]:
    """Return worktree tool specs for registration."""
    from typing import cast

    return [
        cast("ToolSpec", enter_worktree.spec),
        cast("ToolSpec", exit_worktree.spec),
    ]
