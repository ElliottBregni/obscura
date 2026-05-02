"""obscura.tools.worktree_observer — Per-worktree file-change observer.

Wraps :class:`obscura.core.file_watcher.FileWatcher` with a registry keyed
by worktree slug so that ``enter_worktree``/``exit_worktree`` and the agent
isolation lifecycle can start and stop observers without sharing state
through a global singleton instance.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

from obscura.core.file_watcher import FileWatcher

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_observers: dict[str, FileWatcher] = {}


def start(slug: str, worktree_path: str | Path) -> bool:
    """Start watching ``worktree_path`` under the given slug.

    Idempotent — calling twice for the same slug is a no-op and returns ``False``.
    """
    path = Path(worktree_path)
    if not path.is_dir():
        logger.debug("Observer: path %s not a directory, skipping", path)
        return False
    with _LOCK:
        if slug in _observers:
            return False
        watcher = FileWatcher([path])
        try:
            watcher.start()
        except Exception:
            logger.warning("Observer start failed for %s", slug, exc_info=True)
            return False
        _observers[slug] = watcher
        return True


def stop(slug: str) -> bool:
    """Stop the observer for a slug. Returns ``False`` if none was running."""
    with _LOCK:
        watcher = _observers.pop(slug, None)
    if watcher is None:
        return False
    try:
        watcher.stop()
    except Exception:
        logger.debug("Observer stop failed for %s", slug, exc_info=True)
    return True


def summary(slug: str) -> dict[str, Any] | None:
    """Return a summary of changes for a slug, or ``None`` if not active."""
    with _LOCK:
        watcher = _observers.get(slug)
    if watcher is None:
        return None
    return watcher.summary()


def active_slugs() -> list[str]:
    with _LOCK:
        return list(_observers.keys())


def stop_all() -> list[str]:
    """Stop every active observer. Returns the list of slugs that were stopped."""
    with _LOCK:
        items = list(_observers.items())
        _observers.clear()
    stopped: list[str] = []
    for slug, watcher in items:
        try:
            watcher.stop()
            stopped.append(slug)
        except Exception:
            logger.debug("Observer stop_all failed for %s", slug, exc_info=True)
    return stopped


__all__ = [
    "active_slugs",
    "start",
    "stop",
    "stop_all",
    "summary",
]
