"""obscura.kairos.vault_sync — Vault sync engine.

Manages bidirectional sync between the user's vault (``~/.obscura/vault/``)
and Obscura's internal data stores (GoalStore/SQLite, TaskQueue, VectorMemory,
Profile). Goal exports read from GoalStore (kairos.db) as the canonical source,
with GoalBoard (markdown files) as a supplemental fallback for legacy goals.

Zone model:
  - ``vault/user/``   — User-authored. Obscura reads only, never writes.
  - ``vault/agent/``  — Obscura-authored. User reads only.
  - ``vault/shared/`` — Collaborative. Fork-merge on conflict.

File frontmatter drives routing::

    ---
    type: goal          # goal | task | reference | note | profile
    priority: high      # (goals/tasks only)
    status: active      # (goals only)
    ---

    Body text here.

Usage::

    vs = VaultSync()
    vs.bootstrap()           # Create zone dirs on first run
    report = await vs.sync() # Full ingest + export cycle
    vs.status()              # Dict with zone file counts
"""

from __future__ import annotations

import hashlib
import json
import logging
import time as _time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import yaml

logger = logging.getLogger(__name__)


def _empty_str_list() -> list[str]:
    return []


def _empty_file_meta_list() -> list["FileMeta"]:
    return []


def _empty_any_dict() -> dict[str, Any]:
    return {}


def _retry(
    fn: Callable[[], Any],
    *,
    attempts: int = 3,
    base_delay: float = 0.5,
    label: str = "",
) -> Any:
    """Call fn() up to `attempts` times with exponential backoff on exception."""
    last_exc: Exception | None = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            if i < attempts - 1:
                delay = base_delay * (2 ** i)
                logger.debug(
                    "Retry %d/%d for %s after %.1fs: %s", i + 1, attempts, label, delay, exc
                )
                _time.sleep(delay)
    logger.warning("All %d attempts failed for %s: %s", attempts, label, last_exc)
    if last_exc is None:
        raise RuntimeError(f"_retry failed but no exception captured for {label}")
    raise last_exc  # Re-raise so callers can decide to continue or abort


_DEFAULT_VAULT_DIR = Path.home() / ".obscura" / "vault"

# Zone subdirectories created by bootstrap.
_ZONE_DIRS = (
    "user/goals",
    "user/tasks",
    "user/notes",
    "agent/goals",
    "agent/tasks",
    "agent/arbiter",
    "shared/decisions",
    "shared/context",
    "shared/runbooks",
)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class FileMeta:
    """Metadata for a single vault file."""

    path: Path
    owner: str  # "user", "agent", "shared"
    hash: str
    frontmatter: dict[str, Any] = field(default_factory=_empty_any_dict)
    body: str = ""


@dataclass
class ChangeSet:
    """Delta between two vault scans."""

    added: list[FileMeta] = field(default_factory=_empty_file_meta_list)
    modified: list[FileMeta] = field(default_factory=_empty_file_meta_list)
    removed: list[FileMeta] = field(default_factory=_empty_file_meta_list)


@dataclass
class SyncReport:
    """Result of a full sync cycle."""

    ingested: int = 0
    exported: int = 0
    conflicts: int = 0
    errors: list[str] = field(default_factory=_empty_str_list)
    details: dict[str, Any] = field(default_factory=_empty_any_dict)

    def summary(self) -> str:
        parts = [f"ingested={self.ingested}", f"exported={self.exported}"]
        if self.conflicts:
            parts.append(f"conflicts={self.conflicts}")
        if self.errors:
            parts.append(f"errors={len(self.errors)}")
        return f"VaultSync: {', '.join(parts)}"


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------


class VaultSync:
    """Vault sync engine with zone-aware ingest and export."""

    def __init__(
        self,
        vault_dir: Path | str | None = None,
        *,
        project_vault_dir: Path | str | None = None,
        autosync: bool = True,
        dry_run: bool = False,
    ) -> None:
        self.vault_dir = Path(vault_dir or _DEFAULT_VAULT_DIR).expanduser()
        # Optional per-project vault overlay (e.g. <project>/.obscura/vault/).
        # When set, scan() merges both; project files shadow global ones.
        self.project_vault_dir: Path | None = (
            Path(project_vault_dir).expanduser() if project_vault_dir else None
        )
        self.autosync = bool(autosync)
        self.dry_run = bool(dry_run)
        self._prev_hashes: dict[str, str] = {}
        self._hash_file = self.vault_dir / ".sync_state.json"

    # ------------------------------------------------------------------
    # Bootstrap
    # ------------------------------------------------------------------

    def bootstrap(self) -> None:
        """Create zone directory structure and seed files."""
        for zone_dir in _ZONE_DIRS:
            (self.vault_dir / zone_dir).mkdir(parents=True, exist_ok=True)

        # Seed user/profile.md if missing.
        profile = self.vault_dir / "user" / "profile.md"
        if not profile.exists():
            profile.write_text(
                "---\ntype: profile\n---\n\n"
                "# User Profile\n\n"
                "Add your preferences, role, and context here.\n"
                "Obscura reads this on every sync.\n",
                encoding="utf-8",
            )

        # Bootstrap project vault zones if a project overlay is configured.
        if self.project_vault_dir is not None:
            for zone_dir in _ZONE_DIRS:
                (self.project_vault_dir / zone_dir).mkdir(parents=True, exist_ok=True)
            logger.debug("Project vault bootstrapped at %s", self.project_vault_dir)

        # Load previous sync state.
        self._load_hashes()
        logger.debug("Vault bootstrapped at %s", self.vault_dir)

    # ------------------------------------------------------------------
    # Scan & detect
    # ------------------------------------------------------------------

    def scan(self, zone: str = "") -> list[FileMeta]:
        """Discover markdown files under the vault (or a specific zone)."""
        root = self.vault_dir / zone if zone else self.vault_dir
        if not root.exists():
            return []
        metas: list[FileMeta] = []
        for p in sorted(root.rglob("*.md")):
            rel = p.relative_to(self.vault_dir)
            owner = rel.parts[0] if len(rel.parts) > 1 else "shared"
            if owner not in ("user", "agent", "shared"):
                continue  # Skip non-zone files (e.g. obsidian/).
            file_hash = self._compute_hash(p)
            fm, body = self._parse_frontmatter(p)
            metas.append(
                FileMeta(path=p, owner=owner, hash=file_hash, frontmatter=fm, body=body)
            )
        # Merge project vault overlay — project files shadow global ones by rel-path.
        if self.project_vault_dir is not None:
            proj_root = self.project_vault_dir
            proj_scan_root = proj_root / zone if zone else proj_root
            if proj_scan_root.exists():
                global_rels = {m.path.relative_to(self.vault_dir): m for m in metas}
                for p in sorted(proj_scan_root.rglob("*.md")):
                    rel = p.relative_to(proj_root)
                    owner = rel.parts[0] if len(rel.parts) > 1 else "shared"
                    if owner not in ("user", "agent", "shared"):
                        continue
                    file_hash = self._compute_hash(p)
                    fm, body = self._parse_frontmatter(p)
                    proj_meta = FileMeta(
                        path=p, owner=owner, hash=file_hash, frontmatter=fm, body=body
                    )
                    # Project file shadows global file with the same relative path.
                    if rel in global_rels:
                        metas = [m for m in metas if m.path.relative_to(self.vault_dir) != rel]
                    metas.append(proj_meta)

        return metas

    def detect_changes(self, zone: str = "") -> ChangeSet:
        """Compare current scan with previous hashes."""
        current = self.scan(zone)
        current_map = {str(m.path): m for m in current}

        added: list[FileMeta] = []
        modified: list[FileMeta] = []
        removed: list[FileMeta] = []

        for p_str, meta in current_map.items():
            prev = self._prev_hashes.get(p_str)
            if prev is None:
                added.append(meta)
            elif prev != meta.hash:
                modified.append(meta)

        for p_str in list(self._prev_hashes):
            if p_str not in current_map:
                removed.append(
                    FileMeta(path=Path(p_str), owner="unknown", hash="", frontmatter={})
                )

        return ChangeSet(added=added, modified=modified, removed=removed)

    # ------------------------------------------------------------------
    # Sync (full cycle)
    # ------------------------------------------------------------------

    async def sync(self, dry_run: bool = False) -> SyncReport:
        """Run a full sync cycle: ingest user zone → export to agent zone."""
        report = SyncReport()
        effective_dry_run = dry_run or self.dry_run

        # 1. Ingest user/ zone into Obscura data stores.
        user_changes = self.detect_changes("user")
        changed_files = user_changes.added + user_changes.modified
        for meta in changed_files:
            try:
                if not effective_dry_run:
                    _retry(
                        lambda m=meta: self._ingest_file(m),
                        label=f"ingest:{meta.path.name}",
                    )
                report.ingested += 1
            except Exception as exc:
                report.errors.append(f"ingest {meta.path}: {exc}")

        # 2. Export Obscura state to agent/ zone.
        if not effective_dry_run:
            try:
                exported = self._export_all()
                report.exported = exported
            except Exception as exc:
                report.errors.append(f"export: {exc}")

        # 3. Update hash state.
        if not effective_dry_run:
            for meta in self.scan():
                self._prev_hashes[str(meta.path)] = meta.hash
            self._save_hashes()

        report.details = {
            "added": len(user_changes.added),
            "modified": len(user_changes.modified),
            "removed": len(user_changes.removed),
        }
        return report

    # ------------------------------------------------------------------
    # Ingest (user zone → Obscura stores)
    # ------------------------------------------------------------------

    def _ingest_file(self, meta: FileMeta) -> None:
        """Route a user-zone file to the appropriate data store."""
        file_type = str(meta.frontmatter.get("type", "note")).lower()

        if file_type == "goal":
            self._ingest_goal(meta)
        elif file_type == "task":
            self._ingest_task(meta)
        elif file_type == "profile":
            self._ingest_profile(meta)
        elif file_type in ("note", "reference"):
            self._ingest_to_vector(meta, file_type)
        else:
            logger.debug("Skipping unrecognized type '%s': %s", file_type, meta.path)

    def _ingest_goal(self, meta: FileMeta) -> None:
        """Create or update a goal from a user-zone markdown file.

        Policy: user zone always wins.
        If the in-memory (GoalBoard) version of this goal is newer than what the
        user's file records (e.g. the agent wrote progress updates that hadn't been
        synced), we treat that as a conflict.  The user's file still wins — we
        overwrite GoalBoard — but we first archive the agent's version to
        ``vault/agent/goals/.conflicts/<goal_id>.<ISO8601>.md`` so no data is lost.
        """
        from obscura.kairos.goals import GoalBoard

        board = GoalBoard()
        title = str(
            meta.frontmatter.get("title", meta.path.stem.replace("-", " ").title())
        )
        goal_id = meta.path.stem

        # Timestamp declared in the user's file (may be absent for hand-written files).
        user_updated = str(meta.frontmatter.get("updated", ""))

        existing = board.load(goal_id)
        if existing:
            # --- Conflict detection ---
            # "user zone always wins" — but preserve the agent's version first if it
            # contains work that post-dates what the user's file acknowledges.
            if user_updated:
                conflicting = board.get_if_newer(goal_id, since=user_updated)
            else:
                # No timestamp in user file: treat any in-memory version as a
                # potential conflict worth preserving.
                conflicting = existing if existing.updated else None

            if conflicting is not None:
                self._archive_conflict(conflicting)

            # User file wins — overwrite in-memory version unconditionally.
            fields: dict[str, Any] = {}
            if meta.frontmatter.get("priority"):
                fields["priority"] = meta.frontmatter["priority"]
            if meta.frontmatter.get("status"):
                fields["status"] = meta.frontmatter["status"]
            if meta.frontmatter.get("acceptance_criteria"):
                fields["acceptance_criteria"] = meta.frontmatter["acceptance_criteria"]
            if meta.body:
                fields["body"] = meta.body
            if fields:
                board.update(goal_id, **fields)

            logger.info(
                "[vault] Goal %s updated from user zone — in-memory version replaced",
                goal_id,
            )
        else:
            board.create(
                title,
                priority=str(meta.frontmatter.get("priority", "medium")),
                context=meta.body,
                acceptance_criteria=meta.frontmatter.get("acceptance_criteria"),
                status=str(meta.frontmatter.get("status", "active")),
            )
            logger.debug(
                "Vault ingest: created goal %s from %s", goal_id, meta.path.name
            )

    def _archive_conflict(self, goal: Any) -> None:
        """Write a conflicting in-memory goal version to the .conflicts/ directory.

        Path: ``vault/agent/goals/.conflicts/<goal_id>.<ISO8601>.md``

        This preserves agent-side progress (e.g. task linkages, % progress) that
        existed before the user's file was ingested.  The user's version still wins;
        this archive is for auditability and recovery only.
        """
        import yaml as _yaml

        conflicts_dir = self.vault_dir / "agent" / "goals" / ".conflicts"
        conflicts_dir.mkdir(parents=True, exist_ok=True)

        # Build a safe ISO 8601 filename (colons not valid on some FSes).
        ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        out_path = conflicts_dir / f"{goal.id}.{ts}.md"

        data: dict[str, Any] = {
            "id": goal.id,
            "title": goal.title,
            "status": goal.status,
            "priority": goal.priority,
            "created": goal.created,
            "updated": goal.updated,
            "acceptance_criteria": list(goal.acceptance_criteria),
            "depends_on": list(goal.depends_on),
            "tasks": list(goal.tasks),
            "progress": goal.progress,
            "last_worked": goal.last_worked,
            "conflict_archived_at": datetime.now(UTC).isoformat(),
            "conflict_reason": "user zone ingest overwrote newer in-memory version",
        }
        fm = _yaml.dump(data, default_flow_style=False, sort_keys=False)
        content = f"---\n{fm}---\n\n{goal.body or ''}\n"

        try:
            out_path.write_text(content, encoding="utf-8")
            logger.debug("[vault] Conflict archive written: %s", out_path.name)
        except Exception:
            logger.warning(
                "[vault] Could not write conflict archive for %s", goal.id, exc_info=True
            )

    def _ingest_task(self, meta: FileMeta) -> None:
        """Create a task from a user-zone markdown file."""
        from obscura.core.task_queue import TaskQueue

        q = TaskQueue()
        subject = str(
            meta.frontmatter.get("title", meta.path.stem.replace("-", " ").title())
        )
        priority_map = {"critical": 0, "high": 25, "medium": 50, "low": 75}
        priority = priority_map.get(str(meta.frontmatter.get("priority", "medium")), 50)
        goal_id = str(meta.frontmatter.get("goal_id", ""))

        q.enqueue(
            subject,
            description=meta.body,
            priority=priority,
            goal_id=goal_id,
        )
        logger.debug("Vault ingest: created task '%s' from %s", subject, meta.path.name)

    def _ingest_profile(self, meta: FileMeta) -> None:
        """Update user profile from a vault file."""
        try:
            from obscura.auth.context import current_user  # pyright: ignore[reportMissingImports, reportUnknownVariableType]
            from obscura.profile.store import ProfileStore

            user = cast(Any, current_user())
            store: Any = ProfileStore.for_user(user)
            # Parse simple key: value pairs from the body.
            for line in meta.body.splitlines():
                line = line.strip()
                if ":" in line and not line.startswith("#"):
                    key, _, value = line.partition(":")
                    key = key.strip().lower().replace(" ", "_")
                    value = value.strip()
                    if key and value:
                        store.set(key, value)
            logger.debug("Vault ingest: updated profile from %s", meta.path.name)
        except Exception:
            logger.debug(
                "Profile ingest failed (auth context may not be available)",
                exc_info=True,
            )

    def _ingest_to_vector(self, meta: FileMeta, memory_type: str) -> None:
        """Ingest a note/reference file into vector memory."""
        try:
            from obscura.auth.context import current_user  # pyright: ignore[reportMissingImports, reportUnknownVariableType]
            from obscura.vector_memory.vector_memory import VectorMemoryStore

            store = VectorMemoryStore.for_user(cast(Any, current_user()))
            key = f"vault:{meta.owner}:{meta.path.stem}"
            store.set(
                key=key,
                text=meta.body,
                namespace="vault",
                memory_type=memory_type,
                metadata={
                    "source": str(meta.path),
                    "vault_zone": meta.owner,
                    "type": memory_type,
                },
            )
            logger.debug("Vault ingest: stored %s in vector memory", meta.path.name)
        except Exception:
            logger.debug("Vector ingest failed", exc_info=True)

    # ------------------------------------------------------------------
    # Export (Obscura stores → agent zone)
    # ------------------------------------------------------------------

    def _export_all(self) -> int:
        """Export Obscura state to the agent/ zone. Returns file count."""
        count = 0
        for fn, label in (
            (self._export_goals, "export_goals"),
            (self._export_queue_snapshot, "export_queue_snapshot"),
            (self._export_arbiter_verdicts, "export_arbiter_verdicts"),
            (self._export_profile_summary, "export_profile_summary"),
        ):
            try:
                count += _retry(fn, label=label)
            except Exception as exc:
                logger.warning("Export step %s failed after retries: %s", label, exc)
        return count

    def _export_goals(self) -> int:
        """Export active goals to vault/agent/goals/.

        Reads from GoalStore (SQLite kairos.db) as the canonical source.
        Falls back to GoalBoard (markdown files) for goals not yet in SQLite.
        """
        goals_dir = self.vault_dir / "agent" / "goals"
        goals_dir.mkdir(parents=True, exist_ok=True)

        # Clean stale exports.
        for old in goals_dir.glob("*.md"):
            old.unlink()

        count = 0
        exported_ids: set[str] = set()

        # --- Primary source: GoalStore (SQLite) ---
        try:
            from obscura.core.kairos.goal_store import GoalStore
            from obscura.core.paths import resolve_obscura_home

            _TERMINAL_STATUSES = {"completed", "failed", "cancelled"}

            db_path = resolve_obscura_home() / "kairos.db"
            if db_path.exists():
                store = GoalStore(str(db_path))
                try:
                    goals = store.list_goals()
                finally:
                    store.close()

                for goal in goals:
                    status_val = (
                        goal.status.value
                        if hasattr(goal.status, "value")
                        else str(goal.status)
                    )
                    if status_val in _TERMINAL_STATUSES:
                        continue

                    created_iso = (
                        goal.created_at.isoformat()
                        if hasattr(goal.created_at, "isoformat")
                        else str(goal.created_at)
                    )
                    data = {
                        "id": goal.goal_id,
                        "title": goal.title,
                        "status": status_val,
                        "created_at": created_iso,
                        "success_criteria": list(goal.success_criteria),
                        "tags": list(goal.tags),
                    }
                    fm = yaml.dump(data, default_flow_style=False, sort_keys=False)
                    body = goal.description or ""
                    content = f"---\n{fm}---\n\n{body}\n"
                    (goals_dir / f"{goal.goal_id}.md").write_text(
                        content, encoding="utf-8"
                    )
                    exported_ids.add(goal.goal_id)
                    count += 1
        except Exception:
            logger.debug(
                "GoalStore export failed, will fall back to GoalBoard", exc_info=True
            )

        # --- Fallback / supplement: GoalBoard (markdown files) ---
        # Export any goal whose ID isn't already covered by GoalStore above.
        try:
            from obscura.kairos.goals import GoalBoard

            board = GoalBoard()
            for goal in board.load_all():
                if goal.id in exported_ids:
                    continue
                if goal.status in ("completed", "abandoned"):
                    continue
                data = {
                    "id": goal.id,
                    "title": goal.title,
                    "status": goal.status,
                    "priority": goal.priority,
                    "progress": goal.progress,
                    "created_at": goal.created,
                    "updated_at": goal.updated,
                    "acceptance_criteria": list(goal.acceptance_criteria),
                    "tasks": list(goal.tasks),
                }
                fm = yaml.dump(data, default_flow_style=False, sort_keys=False)
                content = f"---\n{fm}---\n\n{goal.body or ''}\n"
                (goals_dir / f"{goal.id}.md").write_text(content, encoding="utf-8")
                count += 1
        except Exception:
            logger.debug("GoalBoard export failed", exc_info=True)

        return count

    def _export_queue_snapshot(self) -> int:
        """Export pending tasks to vault/agent/tasks/queue-snapshot.md."""
        try:
            from obscura.core.task_queue import TaskQueue

            q = TaskQueue()
            snapshot_path = self.vault_dir / "agent" / "tasks" / "queue-snapshot.md"
            snapshot_path.parent.mkdir(parents=True, exist_ok=True)

            depth = q.queue_depth()
            total = sum(depth.values())

            lines = [
                "---",
                "type: queue_snapshot",
                f"generated: {datetime.now(UTC).isoformat()}",
                f"total_pending: {total}",
                "---",
                "",
                "# Task Queue Snapshot",
                "",
                f"**{total} pending tasks** across {len(depth)} priority levels.",
                "",
            ]

            for prio_str, cnt in sorted(depth.items(), key=lambda x: int(x[0])):
                prio_label = {
                    "0": "critical",
                    "25": "high",
                    "50": "medium",
                    "75": "low",
                    "100": "lowest",
                }.get(prio_str, f"p{prio_str}")
                lines.append(f"- **{prio_label}**: {cnt} task(s)")

            content = "\n".join(lines) + "\n"
        except Exception:
            logger.debug("Queue snapshot export failed", exc_info=True)
            return 0

        # Write outside the data-fetch try/except so transient I/O errors can be retried.
        snapshot_path.write_text(content, encoding="utf-8")
        return 1

    def _export_arbiter_verdicts(self) -> int:
        """Export recent Arbiter verdicts to vault/agent/arbiter/."""
        try:
            from obscura.arbiter.store import ArbiterStore

            store = ArbiterStore()
            recent = store.recent(limit=20)
            if not recent:
                return 0

            verdicts_path = self.vault_dir / "agent" / "arbiter" / "latest-verdicts.md"
            verdicts_path.parent.mkdir(parents=True, exist_ok=True)

            stats = store.stats()

            # Compute score trend: avg of last 5 vs previous 5.
            scores = [row.get("composite", 0.0) for row in recent]
            last5 = scores[:5]
            prev5 = scores[5:10]
            avg_last5 = sum(last5) / len(last5) if last5 else 0.0
            avg_prev5 = sum(prev5) / len(prev5) if prev5 else 0.0
            if avg_prev5 and prev5:
                trend_delta = avg_last5 - avg_prev5
                if trend_delta > 0.02:
                    trend_label = f"improving (+{trend_delta:.3f})"
                elif trend_delta < -0.02:
                    trend_label = f"declining ({trend_delta:.3f})"
                else:
                    trend_label = f"stable ({trend_delta:+.3f})"
            else:
                trend_label = "insufficient data"

            lines = [
                "---",
                "type: arbiter_verdicts",
                f"generated: {datetime.now(UTC).isoformat()}",
                "---",
                "",
                "# Recent Arbiter Verdicts",
                "",
                f"**{stats.get('total', 0)} total** evaluations "
                f"(avg score: {stats.get('avg_composite_score', 0):.2f})",
                f"Score trend (last 5 vs prev 5): **{trend_label}**"
                f" — last5={avg_last5:.3f}, prev5={avg_prev5:.3f}",
                "",
            ]

            by_verdict = stats.get("by_verdict", {})
            for v, cnt in sorted(by_verdict.items()):
                lines.append(f"- **{v}**: {cnt}")
            lines.append("")

            for row in recent[:10]:
                verdict = row.get("verdict", "?")
                kind = row.get("kind", "?")
                target = row.get("target_id", "?")
                session = row.get("session_id", "")
                score = row.get("composite", 0)
                feedback = (row.get("feedback") or "")[:80]
                session_suffix = f" session={session}" if session else ""
                lines.append(
                    f"- [{verdict}] {kind} `{target}`{session_suffix}"
                    f" (score={score:.2f}) {feedback}"
                )

            content = "\n".join(lines) + "\n"
        except Exception:
            logger.debug("Arbiter verdict export failed", exc_info=True)
            return 0

        # Write outside the data-fetch try/except so transient I/O errors can be retried.
        verdicts_path.write_text(content, encoding="utf-8")
        return 1

    def _export_profile_summary(self) -> int:
        """Export a profile summary to vault/agent/profile-summary.md."""
        try:
            from obscura.auth.context import current_user  # pyright: ignore[reportMissingImports, reportUnknownVariableType]
            from obscura.profile.builder import ProfileBuilder
            from obscura.profile.store import ProfileStore

            user = cast(Any, current_user())
            store = ProfileStore.for_user(user)
            builder = ProfileBuilder()
            summary = builder.build_summary(store, max_tokens=600)

            if not summary:
                return 0

            out = self.vault_dir / "agent" / "profile-summary.md"
            out.parent.mkdir(parents=True, exist_ok=True)
            content = (
                f"---\ntype: profile_summary\n"
                f"generated: {datetime.now(UTC).isoformat()}\n---\n\n{summary}\n"
            )
        except Exception:
            logger.debug("Profile summary export failed", exc_info=True)
            return 0

        # Write outside the data-fetch try/except so transient I/O errors can be retried.
        out.write_text(content, encoding="utf-8")
        return 1

    # ------------------------------------------------------------------
    # Notifications (called by tools on mutations)
    # ------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        """Return vault status with zone file counts."""
        result: dict[str, Any] = {
            "vault_path": str(self.vault_dir),
            "exists": self.vault_dir.exists(),
            "autosync": self.autosync,
        }
        if self.vault_dir.exists():
            zones: dict[str, int] = {}
            for zone in ("user", "agent", "shared"):
                zone_dir = self.vault_dir / zone
                if zone_dir.exists():
                    zones[zone] = len(list(zone_dir.rglob("*.md")))
                else:
                    zones[zone] = 0
            result["zones"] = zones
        return result

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_hash(path: Path) -> str:
        h = hashlib.sha256()
        try:
            with path.open("rb") as f:
                while True:
                    chunk = f.read(8192)
                    if not chunk:
                        break
                    h.update(chunk)
        except Exception:
            return ""
        return h.hexdigest()

    @staticmethod
    def _parse_frontmatter(path: Path) -> tuple[dict[str, Any], str]:
        """Parse YAML frontmatter from a markdown file."""
        try:
            raw = path.read_text(encoding="utf-8")
        except Exception:
            return {}, ""
        if not raw.startswith("---"):
            return {}, raw
        parts = raw.split("---", 2)
        if len(parts) < 3:  # noqa: PLR2004
            return {}, raw
        fm: dict[str, Any] = {}
        try:
            loaded: object = yaml.safe_load(parts[1])
            if isinstance(loaded, dict):
                fm = cast(dict[str, Any], loaded)
        except Exception:
            fm = {}
        return fm, parts[2].strip()

    def _load_hashes(self) -> None:
        """Load previous sync state from disk."""
        if self._hash_file.exists():
            try:
                self._prev_hashes = json.loads(
                    self._hash_file.read_text(encoding="utf-8")
                )
            except Exception:
                self._prev_hashes = {}

    def _save_hashes(self) -> None:
        """Persist sync state to disk."""
        try:
            self._hash_file.write_text(
                json.dumps(self._prev_hashes, indent=2),
                encoding="utf-8",
            )
        except Exception:
            logger.debug("Could not save sync state", exc_info=True)


# ---------------------------------------------------------------------------
# Module-level notification helpers (called by tools)
# ---------------------------------------------------------------------------

_instance: VaultSync | None = None


def _auto_project_vault_dir(cwd: str | None = None) -> Path | None:
    """Return <cwd>/.obscura/vault if it exists, else None.

    Allows per-project vaults to be discovered automatically when a project
    drops a ``.obscura/vault/`` directory alongside its code.
    """
    import os

    root = Path(cwd or os.getcwd())
    candidate = root / ".obscura" / "vault"
    return candidate if candidate.is_dir() else None


def _get_instance() -> VaultSync:
    global _instance  # noqa: PLW0603
    if _instance is None:
        _instance = VaultSync(project_vault_dir=_auto_project_vault_dir())
    return _instance


def notify_goal_changed(goal_id: str) -> None:
    """Best-effort: flag that a goal changed (triggers export on next sync)."""
    try:
        vs = _get_instance()
        if not vs.vault_dir.exists():
            return
        # Quick re-export just the goal.
        vs._export_goals()  # pyright: ignore[reportPrivateUsage]
    except Exception:
        pass


def notify_profile_changed() -> None:
    """Best-effort: re-export profile summary."""
    try:
        vs = _get_instance()
        if not vs.vault_dir.exists():
            return
        vs._export_profile_summary()  # pyright: ignore[reportPrivateUsage]
    except Exception:
        pass
