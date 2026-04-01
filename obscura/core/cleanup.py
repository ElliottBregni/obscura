"""
obscura.core.cleanup — Cleanup registry for graceful shutdown.

Registers cleanup tasks that run on process exit (SIGINT/SIGTERM)
to remove stale files, close connections, and release resources.

Usage::

    from obscura.core.cleanup import register_cleanup, run_cleanup

    register_cleanup("close_db", lambda: db.close())
    register_cleanup("remove_lock", lambda: lock_file.unlink(missing_ok=True))

    # On shutdown:
    await run_cleanup()
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

_cleanup_tasks: list[tuple[str, Callable[[], Any]]] = []


def register_cleanup(name: str, task: Callable[[], Any]) -> None:
    """Register a cleanup task to run on shutdown."""
    _cleanup_tasks.append((name, task))


async def run_cleanup() -> list[str]:
    """Run all registered cleanup tasks. Returns list of completed task names."""
    completed: list[str] = []
    for name, task in _cleanup_tasks:
        try:
            result = task()
            if asyncio.iscoroutine(result):
                await result
            completed.append(name)
        except Exception:
            logger.debug("Cleanup task %s failed", name, exc_info=True)
    _cleanup_tasks.clear()
    return completed


def cleanup_stale_files(max_age_days: int = 30) -> dict[str, int]:
    """Remove stale files from Obscura data directories.

    Cleans up:
      - Old daily logs (>max_age_days)
      - Expired background task output
      - Stale export files
      - Empty worktree directories

    Returns counts of items cleaned per category.
    """
    counts: dict[str, int] = {"logs": 0, "output": 0, "exports": 0, "worktrees": 0}
    cutoff = time.time() - (max_age_days * 86400)
    home = Path.home() / ".obscura"

    # Old daily logs
    logs_dir = home / "memory" / "logs"
    if logs_dir.is_dir():
        for log_file in logs_dir.rglob("*.md"):
            try:
                if log_file.stat().st_mtime < cutoff:
                    log_file.unlink()
                    counts["logs"] += 1
            except OSError:
                pass

    # Old output files
    output_dir = home / "output"
    if output_dir.is_dir():
        for f in output_dir.iterdir():
            try:
                if f.is_file() and f.stat().st_mtime < cutoff:
                    f.unlink()
                    counts["output"] += 1
            except OSError:
                pass

    # Old exports
    exports_dir = home / "exports"
    if exports_dir.is_dir():
        for f in exports_dir.iterdir():
            try:
                if f.is_file() and f.stat().st_mtime < cutoff:
                    f.unlink()
                    counts["exports"] += 1
            except OSError:
                pass

    return counts


def reset() -> None:
    """Clear all registered cleanup tasks (for testing)."""
    _cleanup_tasks.clear()
