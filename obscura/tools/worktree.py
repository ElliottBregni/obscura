"""obscura.tools.worktree — Git worktree isolation tools.

Provides ``enter_worktree`` and ``exit_worktree`` tools that let agents
work in isolated git worktrees, preventing changes from polluting the
main working tree until explicitly merged.

Checkouts live under ``~/.obscura/worktrees/{repo_hash}/{slug}/``.  State
is persisted via :mod:`obscura.tools.worktree_registry` so crashed
sessions can be swept on next startup, and :mod:`obscura.tools.worktree_observer`
runs a file watcher against each active worktree.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import obscura.tools.worktree_observer as worktree_observer
import obscura.tools.worktree_registry as worktree_registry
from obscura.auth.secrets import safe_subprocess_env
from obscura.core.cleanup import register_cleanup
from obscura.core.tools import tool

if TYPE_CHECKING:
    from obscura.core.types import ToolSpec

logger = logging.getLogger(__name__)

_cleanup_registered = False


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
        env=safe_subprocess_env(),
    )
    stdout, stderr = await proc.communicate()
    return (
        proc.returncode or 0,
        stdout.decode("utf-8", errors="replace").strip(),
        stderr.decode("utf-8", errors="replace").strip(),
    )


async def _main_repo_root() -> tuple[int, str, str]:
    """Resolve the primary worktree root, even if invoked from a linked worktree.

    ``git rev-parse --show-toplevel`` returns the current worktree, which is
    wrong if we're already inside a linked worktree.  We use the common git
    dir (``--git-common-dir``) and walk up from there.
    """
    rc, common_dir, err = await _git(
        "rev-parse", "--path-format=absolute", "--git-common-dir"
    )
    if rc != 0:
        return rc, "", err
    common = Path(common_dir)
    # The common dir is "<main-repo>/.git" for a normal checkout.  When the
    # repo is bare it's "<main-repo>.git"; fall back to --show-toplevel in
    # that edge case.
    if common.name == ".git":
        return 0, str(common.parent), ""
    rc, toplevel, err = await _git("rev-parse", "--show-toplevel")
    return rc, toplevel, err


def _ensure_cleanup_registered() -> None:
    global _cleanup_registered
    if _cleanup_registered:
        return
    try:
        register_cleanup("worktree_observers", worktree_observer.stop_all)
        _cleanup_registered = True
    except Exception:
        logger.debug("Worktree cleanup hook registration failed", exc_info=True)


def _most_recent_active_slug() -> str:
    entries = [e for e in worktree_registry.load() if e.status == "active"]
    entries.sort(key=lambda e: e.created_at, reverse=True)
    return entries[0].slug if entries else ""


@tool(
    "enter_worktree",
    (
        "Create and enter a git worktree for isolated work. "
        "Changes in the worktree do not affect the main working tree. "
        "Use exit_worktree to return. Checkouts live under ~/.obscura/worktrees/."
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
    slug = name.strip() if name else f"obscura-wt-{int(time.time())}"
    if not re.match(r"^[a-zA-Z0-9._-]{1,64}$", slug):
        return _json_error(
            "invalid_name",
            detail="Name must be alphanumeric/dash/dot/underscore, max 64 chars.",
        )

    if worktree_registry.get(slug) is not None:
        return _json_error(
            "slug_in_use",
            detail=f"A worktree named '{slug}' already exists. Exit it first.",
        )

    rc, git_root, err = await _main_repo_root()
    if rc != 0:
        return _json_error("not_a_git_repo", detail=err)

    branch_name = f"worktree/{slug}"
    worktree_path = str(worktree_registry.worktree_path_for(git_root, slug))
    Path(worktree_path).parent.mkdir(parents=True, exist_ok=True)

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

    original_cwd = os.getcwd()
    entry = worktree_registry.WorktreeEntry(
        slug=slug,
        repo_root=git_root,
        repo_hash=worktree_registry.repo_hash(git_root),
        worktree_path=worktree_path,
        branch=branch_name,
        original_cwd=original_cwd,
        owner="tool",
        pid=os.getpid(),
        created_at=time.time(),
    )
    worktree_registry.add(entry)

    _ensure_cleanup_registered()
    observer_started = worktree_observer.start(slug, worktree_path)

    os.chdir(worktree_path)

    return json.dumps(
        {
            "ok": True,
            "slug": slug,
            "worktree_path": worktree_path,
            "branch": branch_name,
            "original_cwd": original_cwd,
            "observer_active": observer_started,
            "message": f"Entered worktree at {worktree_path} on branch {branch_name}",
        },
    )


@tool(
    "exit_worktree",
    (
        "Exit a git worktree and return to the original directory. "
        "Use action='keep' to preserve the worktree, or 'remove' to delete it. "
        "Without 'name' this exits the most recently entered active worktree."
    ),
    {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["keep", "remove"],
                "description": "'keep' preserves worktree+branch; 'remove' deletes them.",
            },
            "name": {
                "type": "string",
                "description": "Slug of the worktree to exit. Defaults to the most recent active one.",
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
    name: str = "",
    discard_changes: bool = False,
) -> str:
    slug = name.strip() or _most_recent_active_slug()
    if not slug:
        return _json_error("not_in_worktree", detail="No active worktree session.")

    entry = worktree_registry.get(slug)
    if entry is None:
        return _json_error(
            "unknown_worktree", detail=f"No worktree registered for slug '{slug}'."
        )

    worktree_path = entry.worktree_path
    branch = entry.branch
    original_cwd = entry.original_cwd
    git_root = entry.repo_root

    result: dict[str, Any] = {
        "ok": True,
        "action": action,
        "slug": slug,
        "worktree_path": worktree_path,
        "branch": branch,
        "original_cwd": original_cwd,
    }

    observer_changes = worktree_observer.summary(slug)
    if observer_changes is not None:
        result["observer_changes"] = observer_changes

    if action == "remove":
        _, status_out, _ = await _git("status", "--porcelain", cwd=worktree_path)
        uncommitted_files = len([ln for ln in status_out.splitlines() if ln.strip()])

        if uncommitted_files > 0 and not discard_changes:
            return _json_error(
                "uncommitted_changes",
                detail=f"{uncommitted_files} uncommitted file(s). Set discard_changes=true to force remove.",
                uncommitted_files=uncommitted_files,
            )

        result["discarded_files"] = uncommitted_files

    worktree_observer.stop(slug)

    try:
        inside_worktree = os.getcwd() == worktree_path or Path(
            os.getcwd()
        ).is_relative_to(Path(worktree_path))
    except (OSError, ValueError):
        inside_worktree = True

    if inside_worktree:
        fallback = original_cwd if Path(original_cwd).is_dir() else git_root
        try:
            os.chdir(fallback)
        except OSError:
            logger.debug("Failed to chdir back to %s", fallback, exc_info=True)

    if action == "remove":
        await _git("worktree", "remove", "--force", worktree_path, cwd=git_root)
        await _git("branch", "-D", branch, cwd=git_root)
        worktree_registry.remove(slug)
        result["message"] = f"Removed worktree and branch {branch}"
    else:
        worktree_registry.update(slug, status="kept")
        result["message"] = f"Kept worktree at {worktree_path} on branch {branch}"

    return json.dumps(result)


def get_worktree_tool_specs() -> list[ToolSpec]:
    """Return worktree tool specs for registration."""
    from typing import cast

    return [
        cast("ToolSpec", getattr(enter_worktree, "spec")),
        cast("ToolSpec", getattr(exit_worktree, "spec")),
    ]
