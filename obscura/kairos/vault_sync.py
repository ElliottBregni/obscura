"""Vault sync engine (skeleton).

Minimal, safe implementation to enable TDD and iteration. Provides
scan/detect/change primitives; ingest/export and conflict resolution
are left as explicit TODOs to implement in T3.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional


@dataclass
class FileMeta:
    path: Path
    owner: str
    hash: str
    frontmatter: Dict[str, object]


@dataclass
class ChangeSet:
    added: List[FileMeta]
    modified: List[FileMeta]
    removed: List[FileMeta]


class VaultSync:
    """Lightweight VaultSync skeleton.

    Methods implemented here are intentionally minimal but correct so
    unit tests can drive further development.
    """

    def __init__(self, vault_dir: Path | str, *, autosync: bool = True, dry_run: bool = False) -> None:
        self.vault_dir = Path(vault_dir).expanduser()
        self.autosync = bool(autosync)
        self.dry_run = bool(dry_run)

    # -- helpers -----------------------------------------------------------------
    def _compute_hash(self, path: Path) -> str:
        h = hashlib.sha256()
        with path.open("rb") as f:
            while True:
                chunk = f.read(8192)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()

    # -- discovery --------------------------------------------------------------
    def scan(self) -> List[FileMeta]:
        """Discover markdown files under the vault and return FileMeta list."""
        metas: List[FileMeta] = []
        if not self.vault_dir.exists():
            return metas
        for p in sorted(self.vault_dir.rglob("*.md")):
            # Simple owner inference from first path component
            rel = p.relative_to(self.vault_dir)
            owner = rel.parts[0] if len(rel.parts) > 1 else "shared"
            try:
                file_hash = self._compute_hash(p)
            except Exception:
                file_hash = ""
            fm = FileMeta(path=p, owner=owner, hash=file_hash, frontmatter={})
            metas.append(fm)
        return metas

    def detect_changes(self, prev_hashes: Optional[Dict[str, str]] = None) -> ChangeSet:
        """Compare current scan with prev_hashes mapping (path -> hash).

        Returns lists of added, modified, and removed FileMeta objects.
        """
        prev_hashes = prev_hashes or {}
        current = self.scan()
        current_map = {str(m.path): m for m in current}

        added: List[FileMeta] = []
        modified: List[FileMeta] = []
        removed: List[FileMeta] = []

        # Added or modified
        for p_str, meta in current_map.items():
            prev = prev_hashes.get(p_str)
            if prev is None:
                added.append(meta)
            elif prev != meta.hash:
                modified.append(meta)

        # Removed
        for p_str in prev_hashes.keys():
            if p_str not in current_map:
                removed.append(FileMeta(path=Path(p_str), owner="unknown", hash="", frontmatter={}))

        return ChangeSet(added=added, modified=modified, removed=removed)

    # -- placeholders (to be implemented) -------------------------------------
    def ingest(self, changes: ChangeSet) -> None:
        """Ingest detected changes into Obscura data stores.

        TODO: implement mapping to GoalBoard/Profile/Vector stores.
        """
        raise NotImplementedError("ingest() not implemented yet")

    def export(self, items: List[object]) -> None:
        """Export Obscura items back to the vault (agent/ zone).

        TODO: implement safe export respecting ownership rules.
        """
        raise NotImplementedError("export() not implemented yet")

    def resolve_conflict(self, path: Path, strategy: str = "fork") -> None:
        """Resolve a conflict for path using given strategy.

        Strategies: fork|prefer_user|prefer_agent|manual
        """
        raise NotImplementedError("resolve_conflict() not implemented yet")

    def sync(self, dry_run: bool = False) -> Dict[str, object]:
        """Run a full sync cycle (scan -> detect -> ingest -> export).

        This skeleton performs only scan/detect and returns a report.
        """
        prev_hashes = {}
        changes = self.detect_changes(prev_hashes)
        return {
            "added": [str(m.path) for m in changes.added],
            "modified": [str(m.path) for m in changes.modified],
            "removed": [str(m.path) for m in changes.removed],
        }

    def status(self) -> Dict[str, object]:
        return {"vault_dir": str(self.vault_dir), "autosync": self.autosync}
