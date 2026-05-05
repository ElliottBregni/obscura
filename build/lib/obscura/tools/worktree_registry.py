"""obscura.tools.worktree_registry — Persistent manifest for active worktrees.

Worktrees live under ``~/.obscura/worktrees/{repo_hash}/{slug}/``.  The
registry at ``~/.obscura/worktrees/registry.json`` records each checkout so
that crashed sessions can be detected and cleaned up on next startup.

Entries are keyed by slug (globally unique across repos — the slug defaults
to ``obscura-wt-{ts}`` or ``agent-{name}`` and collisions are rejected).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from obscura.core.enums.lifecycle import WorktreeStatus
from obscura.core.models.lifecycle import WorktreeEntry as WorktreeEntry

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()


def registry_root() -> Path:
    """Directory that holds the registry and all checkouts."""
    return Path.home() / ".obscura" / "worktrees"


def registry_path() -> Path:
    return registry_root() / "registry.json"


def repo_hash(repo_root: str | Path) -> str:
    """Stable 12-char hash of an absolute repo path."""
    resolved = str(Path(repo_root).resolve())
    return hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:12]


def worktree_path_for(repo_root: str | Path, slug: str) -> Path:
    """Return the on-disk checkout path for ``(repo_root, slug)``."""
    return registry_root() / repo_hash(repo_root) / slug


def _empty_entries() -> list[WorktreeEntry]:
    return []


@dataclass
class _Manifest:
    entries: list[WorktreeEntry] = field(default_factory=_empty_entries)


def _load_locked() -> _Manifest:
    path = registry_path()
    if not path.is_file():
        return _Manifest()
    try:
        raw = cast("object", json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        logger.warning("Worktree registry unreadable; starting fresh", exc_info=True)
        return _Manifest()
    if not isinstance(raw, dict):
        return _Manifest()
    raw_dict = cast("dict[str, Any]", raw)
    items_raw = raw_dict.get("entries", [])
    if not isinstance(items_raw, list):
        return _Manifest()
    items_list = cast("list[Any]", items_raw)
    entries: list[WorktreeEntry] = []
    for item in items_list:
        if isinstance(item, dict):
            entries.append(WorktreeEntry.from_row(cast("dict[str, Any]", item)))
    return _Manifest(entries=entries)


def _save_locked(manifest: _Manifest) -> None:
    path = registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"entries": [e.to_row() for e in manifest.entries]}
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def load() -> list[WorktreeEntry]:
    with _LOCK:
        return list(_load_locked().entries)


def get(slug: str) -> WorktreeEntry | None:
    with _LOCK:
        for entry in _load_locked().entries:
            if entry.slug == slug:
                return entry
    return None


def add(entry: WorktreeEntry) -> None:
    with _LOCK:
        manifest = _load_locked()
        manifest.entries = [e for e in manifest.entries if e.slug != entry.slug]
        manifest.entries.append(entry)
        _save_locked(manifest)


def update(slug: str, **fields: Any) -> WorktreeEntry | None:
    with _LOCK:
        manifest = _load_locked()
        updated: WorktreeEntry | None = None
        for entry in manifest.entries:
            if entry.slug == slug:
                for key, value in fields.items():
                    if hasattr(entry, key):
                        setattr(entry, key, value)
                updated = entry
                break
        if updated is not None:
            _save_locked(manifest)
        return updated


def remove(slug: str) -> bool:
    with _LOCK:
        manifest = _load_locked()
        before = len(manifest.entries)
        manifest.entries = [e for e in manifest.entries if e.slug != slug]
        if len(manifest.entries) == before:
            return False
        _save_locked(manifest)
        return True


def list_for_repo(repo_root: str | Path) -> list[WorktreeEntry]:
    target = repo_hash(repo_root)
    return [e for e in load() if e.repo_hash == target]


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        logger.debug("suppressed exception in _pid_alive", exc_info=True)
        return False
    except PermissionError:
        logger.debug("suppressed exception in _pid_alive", exc_info=True)
        return True
    except OSError:
        logger.debug("suppressed exception in _pid_alive", exc_info=True)
        return False
    return True


def sweep_dead_pids() -> list[WorktreeEntry]:
    """Mark active entries whose owning process has exited as orphan."""
    orphans: list[WorktreeEntry] = []
    with _LOCK:
        manifest = _load_locked()
        changed = False
        for entry in manifest.entries:
            if entry.status == WorktreeStatus.ACTIVE and not _pid_alive(entry.pid):
                entry.status = WorktreeStatus.ORPHAN
                orphans.append(entry)
                changed = True
        if changed:
            _save_locked(manifest)
    return orphans


def prune_missing_paths() -> list[str]:
    """Drop registry entries whose worktree directory no longer exists."""
    dropped: list[str] = []
    with _LOCK:
        manifest = _load_locked()
        kept: list[WorktreeEntry] = []
        for entry in manifest.entries:
            if (
                entry.status == WorktreeStatus.KEPT
                or Path(entry.worktree_path).exists()
            ):
                kept.append(entry)
            else:
                dropped.append(entry.slug)
        if dropped:
            manifest.entries = kept
            _save_locked(manifest)
    return dropped


def cleanup_orphan_dirs() -> int:
    """Remove on-disk checkouts that are not referenced by the registry.

    Only removes directories that look like worktree checkouts (contain a
    ``.git`` file pointing at a gitdir).  Returns count removed.
    """
    root = registry_root()
    if not root.is_dir():
        return 0
    known = {Path(e.worktree_path).resolve() for e in load()}
    removed = 0
    for repo_dir in root.iterdir():
        if not repo_dir.is_dir():
            continue
        for slug_dir in repo_dir.iterdir():
            if not slug_dir.is_dir():
                continue
            resolved = slug_dir.resolve()
            if resolved in known:
                continue
            git_marker = slug_dir / ".git"
            if not git_marker.exists():
                continue
            try:
                shutil.rmtree(slug_dir)
                removed += 1
            except OSError:
                logger.debug(
                    "Failed to remove orphan worktree %s", slug_dir, exc_info=True
                )
    return removed


__all__ = [
    "WorktreeEntry",
    "add",
    "cleanup_orphan_dirs",
    "get",
    "list_for_repo",
    "load",
    "prune_missing_paths",
    "registry_path",
    "registry_root",
    "remove",
    "repo_hash",
    "sweep_dead_pids",
    "update",
    "worktree_path_for",
]
