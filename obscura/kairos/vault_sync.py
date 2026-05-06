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

import asyncio
import concurrent.futures
import contextlib
import hashlib
import json
import logging
import os
import time as _time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import yaml

from obscura.arbiter.store import ArbiterStore
from obscura.auth.cli_user import current_cli_user
from obscura.core.kairos.goal_store import create_goal_store
from obscura.core.paths import resolve_obscura_home
from obscura.core.task_queue import TaskQueue
from obscura.kairos.goals import GoalBoard, is_valid_status_transition
from obscura.profile.builder import ProfileBuilder
from obscura.profile.store import ProfileStore
from obscura.vector_memory.vector_memory import VectorMemoryStore

logger = logging.getLogger(__name__)


def _empty_str_list() -> list[str]:
    return []


def _empty_file_meta_list() -> list["FileMeta"]:
    return []


def _empty_any_dict() -> dict[str, Any]:
    return {}


def _run_async[T](coro: Awaitable[T]) -> T:
    """Run *coro* to completion from synchronous code.

    The export pipeline is sync (``_export_all`` calls each step
    synchronously) but is itself dispatched from
    ``VaultSync.sync()`` which is async — so an event loop is already
    running on this thread. ``asyncio.run`` raises ``RuntimeError`` in
    that case and the un-awaited coroutine leaks (visible as the
    ``coroutine 'list_sessions' was never awaited`` warning).

    Detect a running loop and, when present, drive *coro* on a worker
    thread's own loop. Without a running loop, fall back to
    ``asyncio.run`` (the simple path that tests / CLI scripts hit).
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(cast("Any", coro))
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(asyncio.run, cast("Any", coro))
        return cast("T", future.result())


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
                delay = base_delay * (2**i)
                logger.debug(
                    "Retry %d/%d for %s after %.1fs: %s",
                    i + 1,
                    attempts,
                    label,
                    delay,
                    exc,
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
    "shared/sessions",
)


# Caps for the session-log export. Tunable per-deployment without a
# code change because the sync runs unattended and a runaway session
# table shouldn't blow up the vault.
_SESSION_PAGE_CAP = 50  # per-session pages kept on disk
_DIGEST_RECENT_SESSIONS = 20  # rows shown in the digest body
_DIGEST_LOG_TAIL = 500  # deep-log lines scanned for the digest stats


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
        # (goal_id, requested_status) pairs we've already info-logged about
        # being illegal transitions. Without this, every sync tick re-logs
        # the same WARNING as the in-memory FSM rejects the bad request.
        self._logged_invalid_transitions: set[tuple[str, str]] = set()

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
                        metas = [
                            m
                            for m in metas
                            if m.path.relative_to(self.vault_dir) != rel
                        ]
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
                logger.debug("suppressed exception in sync", exc_info=True)
                report.errors.append(f"ingest {meta.path}: {exc}")

        # 2. Export Obscura state to agent/ zone.
        if not effective_dry_run:
            try:
                exported = self._export_all()
                report.exported = exported
            except Exception as exc:
                logger.debug("suppressed exception in sync", exc_info=True)
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

            # User file wins for content — but the lifecycle FSM still
            # governs the status field. If the user's file requests an
            # illegal transition (e.g. completed → in_progress because
            # the markdown is older than the in-memory completion), we
            # skip the status update rather than firing the GoalBoard
            # validator's WARNING on every sync tick.
            fields: dict[str, Any] = {}
            if meta.frontmatter.get("priority"):
                fields["priority"] = meta.frontmatter["priority"]
            requested_status = meta.frontmatter.get("status")
            if requested_status:
                if is_valid_status_transition(existing.status, str(requested_status)):
                    fields["status"] = requested_status
                else:
                    pair = (goal_id, str(requested_status))
                    if pair not in self._logged_invalid_transitions:
                        logger.info(
                            "[vault] Skipping illegal status transition for goal %s: "
                            "file requests %r but in-memory status is %r. "
                            "Update the markdown frontmatter to clear this notice.",
                            goal_id,
                            requested_status,
                            existing.status,
                        )
                        self._logged_invalid_transitions.add(pair)
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
                "[vault] Could not write conflict archive for %s",
                goal.id,
                exc_info=True,
            )

    def _ingest_task(self, meta: FileMeta) -> None:
        """Create a task from a user-zone markdown file.

        Vault sync runs on every tick over the same files, so the enqueue
        is keyed on the source path (the natural fingerprint here). Two
        ticks → one open task, not two duplicates.
        """
        q = TaskQueue()
        subject = str(
            meta.frontmatter.get("title", meta.path.stem.replace("-", " ").title())
        )
        priority_map = {"critical": 0, "high": 25, "medium": 50, "low": 75}
        priority = priority_map.get(str(meta.frontmatter.get("priority", "medium")), 50)
        goal_id = str(meta.frontmatter.get("goal_id", ""))
        # Each markdown file maps to exactly one open task — key on
        # absolute path (resilient to title edits between ticks).
        dedupe_key = f"vault-task|{meta.path.resolve()}"

        q.enqueue(
            subject,
            description=meta.body,
            priority=priority,
            goal_id=goal_id,
            dedupe_key=dedupe_key,
        )
        logger.debug("Vault ingest: created task '%s' from %s", subject, meta.path.name)

    def _ingest_profile(self, meta: FileMeta) -> None:
        """Update user profile from a vault file."""
        try:
            store: Any = ProfileStore.for_user(current_cli_user())
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
            store = VectorMemoryStore.for_user(current_cli_user())
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
    # Page rendering — single source of truth for vault frontmatter
    # ------------------------------------------------------------------

    @staticmethod
    def _render_page(
        *,
        page_type: str,
        page_id: str,
        body: str,
        created_at: str = "",
        updated_at: str = "",
        generated_at: str = "",
        extra_fields: dict[str, Any] | None = None,
    ) -> str:
        """Render a vault page with consistent frontmatter.

        Two timestamp conventions:
          - Entity pages (a goal, a task, a profile)  → ``created_at``
            and ``updated_at``. ``generated_at`` is omitted.
          - Snapshot pages (queue depth, latest verdicts) → ``generated_at``
            only. ``created_at`` / ``updated_at`` are omitted.

        Frontmatter ordering is fixed so consecutive sync ticks produce
        byte-identical output when nothing changed (no spurious diffs).

        ``extra_fields`` keys are appended after the canonical block in
        their dict order. Values must be YAML-serializable.
        """
        ordered: dict[str, Any] = {"id": page_id, "type": page_type}
        if generated_at:
            ordered["generated_at"] = generated_at
        if created_at:
            ordered["created_at"] = created_at
        if updated_at:
            ordered["updated_at"] = updated_at
        if extra_fields:
            for key, val in extra_fields.items():
                if key in ordered:
                    continue
                ordered[key] = val
        fm = yaml.dump(ordered, default_flow_style=False, sort_keys=False)
        body_text = body.rstrip()
        return f"---\n{fm}---\n\n{body_text}\n" if body_text else f"---\n{fm}---\n"

    # ------------------------------------------------------------------
    # Export (Obscura stores → agent zone)
    # ------------------------------------------------------------------

    def _export_all(self) -> int:
        """Export Obscura state to the agent/ + shared/ zones.

        Returns total file count written. Each step is wrapped in
        :func:`_retry` so a single transient I/O failure (e.g. fsync on
        a busy disk) doesn't kill the whole export cycle.
        """
        count = 0
        for fn, label in (
            (self._export_goals, "export_goals"),
            (self._export_queue_snapshot, "export_queue_snapshot"),
            (self._export_arbiter_verdicts, "export_arbiter_verdicts"),
            (self._export_profile_summary, "export_profile_summary"),
            (self.export_session_logs, "export_session_logs"),
        ):
            try:
                count += _retry(fn, label=label)
            except Exception as exc:
                logger.warning("Export step %s failed after retries: %s", label, exc)
        return count

    def _export_goals(self) -> int:
        """Export active goals to vault/agent/goals/.

        Reads from GoalStore (SQLite kairos.db) as the canonical source.
        Falls back to GoalBoard (markdown files) for goals not yet in
        SQLite. Each page goes through :meth:`_render_page` so the
        frontmatter shape is uniform regardless of source.

        Stale files (goals that no longer exist) are removed AFTER we've
        written the live set, so other vault pages with backlinks to a
        goal page won't observe a deletion mid-sync.
        """
        goals_dir = self.vault_dir / "agent" / "goals"
        goals_dir.mkdir(parents=True, exist_ok=True)

        count = 0
        exported_ids: set[str] = set()

        # --- Primary source: GoalStore (SQLite) ---
        try:
            _TERMINAL_STATUSES = {"completed", "failed", "cancelled"}

            db_path = resolve_obscura_home() / "kairos.db"
            if db_path.exists():
                store = create_goal_store(db_path)
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
                    # KAIROS Goals don't carry a separate updated_at —
                    # the closest semantic timestamp is the most recent
                    # state-change marker, which for an active goal is
                    # started_at (work began) when set.
                    updated_iso = ""
                    started_at = getattr(goal, "started_at", None)
                    if started_at is not None:
                        updated_iso = (
                            started_at.isoformat()
                            if hasattr(started_at, "isoformat")
                            else str(started_at)
                        )
                    extra: dict[str, Any] = {
                        "title": goal.title,
                        "status": status_val,
                        "success_criteria": list(goal.success_criteria),
                        "tags": list(goal.tags),
                    }
                    content = self._render_page(
                        page_type="goal",
                        page_id=goal.goal_id,
                        body=goal.description or "",
                        created_at=created_iso,
                        updated_at=updated_iso,
                        extra_fields=extra,
                    )
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
        try:
            board = GoalBoard()
            for goal in board.load_all():
                if goal.id in exported_ids:
                    continue
                if goal.status in ("completed", "abandoned"):
                    continue
                # Render linked tasks as a body section if any exist —
                # currently no per-task page, so we list IDs as plain
                # bullets. When per-task vault pages land, swap the
                # bullet body for [[../tasks/<task_id>]] backlinks.
                body_parts: list[str] = []
                if goal.body:
                    body_parts.append(goal.body)
                if goal.tasks:
                    body_parts.append("## Linked tasks")
                    body_parts.extend(f"- {tid}" for tid in goal.tasks)
                body = "\n\n".join(body_parts)

                extra = {
                    "title": goal.title,
                    "status": goal.status,
                    "priority": goal.priority,
                    "progress": goal.progress,
                    "acceptance_criteria": list(goal.acceptance_criteria),
                    "tasks": list(goal.tasks),
                }
                content = self._render_page(
                    page_type="goal",
                    page_id=goal.id,
                    body=body,
                    created_at=goal.created,
                    updated_at=goal.updated,
                    extra_fields=extra,
                )
                (goals_dir / f"{goal.id}.md").write_text(content, encoding="utf-8")
                exported_ids.add(goal.id)
                count += 1
        except Exception:
            logger.debug("GoalBoard export failed", exc_info=True)

        # Sweep stale exports — files for goal IDs we did NOT just write.
        # Done after writes so backlinks to live goals stay valid all the
        # way through the export cycle.
        for old in goals_dir.glob("*.md"):
            if old.stem not in exported_ids:
                with contextlib.suppress(OSError):
                    old.unlink()

        return count

    def _export_queue_snapshot(self) -> int:
        """Export pending tasks to vault/agent/tasks/queue-snapshot.md.

        The snapshot lists priority-bucket counts AND, when there are
        few enough pending tasks, the top items linked back to their
        goal page (``[[../goals/<goal_id>]]``). This gives the user a
        navigable entry point from the snapshot to the goal context.
        """
        try:
            q = TaskQueue()
            snapshot_path = self.vault_dir / "agent" / "tasks" / "queue-snapshot.md"
            snapshot_path.parent.mkdir(parents=True, exist_ok=True)

            depth = q.queue_depth()
            total = sum(depth.values())
            now_iso = datetime.now(UTC).isoformat()

            body_lines: list[str] = [
                "# Task Queue Snapshot",
                "",
                f"**{total} pending tasks** across {len(depth)} priority levels.",
                "",
                "## By priority",
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
                body_lines.append(f"- **{prio_label}**: {cnt} task(s)")

            # Top pending tasks with goal-page backlinks. Cap so the
            # snapshot stays readable even when the queue is very deep.
            top_tasks = self._top_pending_tasks(q, limit=20)
            if top_tasks:
                body_lines.extend(["", "## Top pending", ""])
                for task in top_tasks:
                    subject = str(task.get("subject", "")).strip() or task.get(
                        "task_id", "?"
                    )
                    goal_id = str(task.get("goal_id", "")).strip()
                    backlink = f" → [[../goals/{goal_id}]]" if goal_id else ""
                    body_lines.append(
                        f"- `{task.get('task_id', '?')}` {subject}{backlink}"
                    )

            content = self._render_page(
                page_type="queue_snapshot",
                page_id="queue-snapshot",
                body="\n".join(body_lines),
                generated_at=now_iso,
                extra_fields={
                    "total_pending": total,
                    "priority_buckets": {k: v for k, v in depth.items()},
                },
            )
        except Exception:
            logger.debug("Queue snapshot export failed", exc_info=True)
            return 0

        # Write outside the data-fetch try/except so transient I/O errors can be retried.
        snapshot_path.write_text(content, encoding="utf-8")
        return 1

    @staticmethod
    def _top_pending_tasks(_q: TaskQueue, *, limit: int = 20) -> list[dict[str, Any]]:
        """Return up to *limit* highest-priority pending tasks.

        Reads directly through the queue's internal connection helper
        (the *_q* arg is held for API symmetry with future TaskQueue
        refactors that expose a public "list pending" method) — for now,
        TaskQueue's public surface only offers next_ready (which claims),
        so we open a read-only connection. The select is bounded by
        *limit* so deep queues stay readable in the snapshot.
        """
        try:
            from obscura.core import task_queue as _tq
            from obscura.core.enums.lifecycle import TaskQueueStatus

            conn = _tq._open()  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
            try:
                rows = conn.execute(
                    "SELECT task_id, subject, goal_id, priority "
                    "FROM tasks WHERE status = ? "
                    "ORDER BY priority ASC, created_at ASC LIMIT ?",
                    (TaskQueueStatus.PENDING.value, limit),
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()
        except Exception:
            logger.debug("top_pending_tasks read failed", exc_info=True)
            return []

    def _export_arbiter_verdicts(self) -> int:
        """Export recent Arbiter verdicts to vault/agent/arbiter/."""
        try:
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

            now_iso = datetime.now(UTC).isoformat()
            body_lines = [
                "# Recent Arbiter Verdicts",
                "",
                f"**{stats.get('total', 0)} total** evaluations "
                f"(avg score: {stats.get('avg_composite_score', 0):.2f})",
                f"Score trend (last 5 vs prev 5): **{trend_label}**"
                f" — last5={avg_last5:.3f}, prev5={avg_prev5:.3f}",
                "",
                "## By verdict",
                "",
            ]

            by_verdict = stats.get("by_verdict", {})
            for v, cnt in sorted(by_verdict.items()):
                body_lines.append(f"- **{v}**: {cnt}")
            body_lines.extend(["", "## Recent", ""])

            for row in recent[:10]:
                verdict = row.get("verdict", "?")
                kind = row.get("kind", "?")
                target = row.get("target_id", "?")
                session = row.get("session_id", "")
                score = row.get("composite", 0)
                feedback = (row.get("feedback") or "")[:80]
                session_suffix = f" session=`{session}`" if session else ""
                body_lines.append(
                    f"- [{verdict}] {kind} `{target}`{session_suffix}"
                    f" (score={score:.2f}) {feedback}"
                )

            content = self._render_page(
                page_type="arbiter_verdicts",
                page_id="latest-verdicts",
                body="\n".join(body_lines),
                generated_at=now_iso,
                extra_fields={
                    "total_evaluations": stats.get("total", 0),
                    "avg_composite_score": stats.get("avg_composite_score", 0),
                    "trend": trend_label,
                },
            )
        except Exception:
            logger.debug("Arbiter verdict export failed", exc_info=True)
            return 0

        # Write outside the data-fetch try/except so transient I/O errors can be retried.
        verdicts_path.write_text(content, encoding="utf-8")
        return 1

    def _export_profile_summary(self) -> int:
        """Export a profile summary to vault/agent/profile-summary.md."""
        try:
            store = ProfileStore.for_user(current_cli_user())
            builder = ProfileBuilder()
            summary = builder.build_summary(store, max_tokens=600)

            if not summary:
                return 0

            out = self.vault_dir / "agent" / "profile-summary.md"
            out.parent.mkdir(parents=True, exist_ok=True)
            content = self._render_page(
                page_type="profile_summary",
                page_id="profile-summary",
                body=summary,
                generated_at=datetime.now(UTC).isoformat(),
            )
        except Exception:
            logger.debug("Profile summary export failed", exc_info=True)
            return 0

        # Write outside the data-fetch try/except so transient I/O errors can be retried.
        out.write_text(content, encoding="utf-8")
        return 1

    # ------------------------------------------------------------------
    # Session log export — per-session pages + rolling digest
    # ------------------------------------------------------------------

    def export_session_logs(self) -> int:
        """Public entry point: dump session logs to vault/shared/sessions/.

        Writes up to ``_SESSION_PAGE_CAP`` per-session pages PLUS one
        rolling ``recent-activity.md`` digest. Both run on every sync
        tick and on-demand via the ``/vault dump-sessions`` slash
        command.

        Returns the total number of files written (per-session + digest).
        """
        return self._export_session_pages() + self._export_session_digest()

    def _export_session_pages(self) -> int:
        """Write one markdown page per session into vault/shared/sessions/.

        Source: event store's :meth:`list_sessions` (most recently
        updated first). Per-session pages contain frontmatter with
        backend / model / project / status / message_count / metadata
        scalars, plus a body that summarizes turn count, tools used
        (from event store events), and a backlink to the linked goal
        page when the session metadata records a goal_id.

        Stale per-session pages (sessions evicted from the cap window
        or removed from the store) are swept AFTER writes so backlinks
        from the digest stay valid mid-export.
        """
        sessions_dir = self.vault_dir / "shared" / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)

        try:
            from obscura.core.db_factory import DatabaseFactory

            store = DatabaseFactory.create_event_store()
        except Exception:
            logger.debug("Session-log export: event store unavailable", exc_info=True)
            return 0

        try:
            sessions = _run_async(store.list_sessions())
        except Exception:
            logger.debug("Session-log export: list_sessions failed", exc_info=True)
            return 0
        finally:
            with contextlib.suppress(Exception):
                store.close()

        # Most recent first; cap so the directory stays bounded.
        try:
            sessions.sort(
                key=lambda s: (
                    getattr(s, "updated_at", None)
                    or getattr(s, "created_at", None)
                    or datetime.min.replace(tzinfo=UTC)
                ),
                reverse=True,
            )
        except Exception:
            logger.debug(
                "Session-log export: sort failed; using natural order", exc_info=True
            )
        live_ids: set[str] = set()
        count = 0
        for sess in sessions[:_SESSION_PAGE_CAP]:
            try:
                page = self._render_session_page(sess)
                if page is None:
                    continue
                page_id, content = page
                (sessions_dir / f"{page_id}.md").write_text(content, encoding="utf-8")
                live_ids.add(page_id)
                count += 1
            except Exception:
                logger.debug(
                    "Session-log export: render failed for session %s",
                    getattr(sess, "id", "?"),
                    exc_info=True,
                )

        # Sweep stale per-session pages — anything not in live_ids and not
        # the digest itself.
        for old in sessions_dir.glob("*.md"):
            if old.stem in live_ids or old.name == "recent-activity.md":
                continue
            with contextlib.suppress(OSError):
                old.unlink()

        return count

    def _render_session_page(self, sess: Any) -> tuple[str, str] | None:
        """Render a single session into (page_id, file_content)."""
        sid = getattr(sess, "id", "") or ""
        if not sid:
            return None

        # Pull events to summarize tools used + turn count. Best-effort —
        # if the per-session query fails we still emit the metadata page.
        tool_counts: dict[str, int] = {}
        turn_count = 0
        try:
            from obscura.core.db_factory import DatabaseFactory
            from obscura.core.enums.agent import AgentEventKind

            store = DatabaseFactory.create_event_store()
            try:
                events = _run_async(store.get_events(sid))
            finally:
                with contextlib.suppress(Exception):
                    store.close()
            for ev in events:
                if ev.kind == AgentEventKind.TURN_COMPLETE:
                    turn_count += 1
                elif ev.kind == AgentEventKind.TOOL_CALL:
                    name = str(ev.payload.get("tool_name", "")).strip() or "?"
                    tool_counts[name] = tool_counts.get(name, 0) + 1
        except Exception:
            logger.debug(
                "Session-log export: event scan failed for %s", sid, exc_info=True
            )

        metadata = dict(getattr(sess, "metadata", {}) or {})
        goal_id = str(metadata.get("goal_id", "")).strip()
        status = getattr(sess, "status", None)
        status_val = getattr(status, "value", None) or str(status or "")
        created = getattr(sess, "created_at", None)
        updated = getattr(sess, "updated_at", None)
        created_iso = (
            created.isoformat()
            if created is not None and hasattr(created, "isoformat")
            else ""
        )
        updated_iso = (
            updated.isoformat()
            if updated is not None and hasattr(updated, "isoformat")
            else ""
        )

        body_lines: list[str] = []
        title = (
            (getattr(sess, "summary", "") or "").strip()
            or getattr(sess, "active_agent", "")
            or sid
        )
        body_lines.append(f"# Session `{sid}`")
        body_lines.append("")
        body_lines.append(title if title != sid else "_(no summary recorded)_")
        body_lines.append("")
        body_lines.append("## Stats")
        body_lines.append("")
        body_lines.append(f"- turns: **{turn_count}**")
        body_lines.append(f"- tool calls: **{sum(tool_counts.values())}**")
        body_lines.append(f"- messages: **{getattr(sess, 'message_count', 0) or 0}**")
        if tool_counts:
            body_lines.append("")
            body_lines.append("## Tools used")
            body_lines.append("")
            for name, cnt in sorted(
                tool_counts.items(), key=lambda kv: (-kv[1], kv[0])
            ):
                body_lines.append(f"- `{name}` — {cnt}")
        if goal_id:
            body_lines.append("")
            body_lines.append("## Goal")
            body_lines.append("")
            body_lines.append(f"- [[../../agent/goals/{goal_id}]]")

        # Frontmatter — strip metadata to scalar/list values so YAML
        # dump stays readable. Drop nested dicts (they bloat the page).
        flat_meta: dict[str, Any] = {}
        for k, v in metadata.items():
            if isinstance(v, (str, int, float, bool)):
                flat_meta[k] = v
            elif isinstance(v, list):
                flat_meta[k] = v[:20]  # cap deep lists like 'turns'

        extra: dict[str, Any] = {
            "agent": getattr(sess, "active_agent", ""),
            "backend": getattr(sess, "backend", ""),
            "model": getattr(sess, "model", ""),
            "project": getattr(sess, "project", ""),
            "status": status_val,
            "message_count": getattr(sess, "message_count", 0) or 0,
            "turn_count": turn_count,
            "tool_call_count": sum(tool_counts.values()),
        }
        if goal_id:
            extra["goal_id"] = goal_id
        if flat_meta:
            extra["metadata"] = flat_meta

        content = self._render_page(
            page_type="session",
            page_id=sid,
            body="\n".join(body_lines),
            created_at=created_iso,
            updated_at=updated_iso,
            extra_fields=extra,
        )
        return sid, content

    def _export_session_digest(self) -> int:
        """Write a single rolling activity digest at recent-activity.md.

        Combines two sources:

          * Event store — most recent N sessions, with backlinks to
            their per-session pages. This gives the human reader an
            entry point into the per-session detail.
          * Deep log JSONL — tail-scan to count tool_call / api_request
            / error entries and surface their totals. This gives the
            "what's been happening lately" view that the per-session
            list can't (it's bounded by session boundaries).
        """
        digest_path = self.vault_dir / "shared" / "sessions" / "recent-activity.md"
        digest_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            from obscura.core.db_factory import DatabaseFactory

            store = DatabaseFactory.create_event_store()
            try:
                sessions = _run_async(store.list_sessions())
            finally:
                with contextlib.suppress(Exception):
                    store.close()
        except Exception:
            logger.debug("Session digest: event store unavailable", exc_info=True)
            sessions = []

        try:
            sessions.sort(
                key=lambda s: (
                    getattr(s, "updated_at", None)
                    or getattr(s, "created_at", None)
                    or datetime.min.replace(tzinfo=UTC)
                ),
                reverse=True,
            )
        except Exception:
            logger.debug("Session digest: sort failed", exc_info=True)

        log_stats = self._scan_deep_log_tail(_DIGEST_LOG_TAIL)

        body_lines: list[str] = ["# Recent activity", ""]
        body_lines.append(
            f"Window: last {_DIGEST_RECENT_SESSIONS} sessions, last "
            f"{_DIGEST_LOG_TAIL} deep-log entries.",
        )
        body_lines.append("")
        body_lines.append("## Deep-log totals")
        body_lines.append("")
        if log_stats["scanned"] == 0:
            body_lines.append("_no deep-log entries available_")
        else:
            body_lines.append(f"- entries scanned: **{log_stats['scanned']}**")
            body_lines.append(f"- tool calls: **{log_stats['tool_calls']}**")
            body_lines.append(f"- API requests: **{log_stats['api_requests']}**")
            body_lines.append(f"- errors: **{log_stats['errors']}**")
            top_tools = sorted(
                log_stats["by_tool"].items(),
                key=lambda kv: (-kv[1], kv[0]),
            )[:10]
            if top_tools:
                body_lines.append("")
                body_lines.append("**Top tools:**")
                body_lines.extend(f"- `{name}` — {cnt}" for name, cnt in top_tools)

        body_lines.append("")
        body_lines.append("## Recent sessions")
        body_lines.append("")
        if not sessions:
            body_lines.append("_no sessions recorded_")
        else:
            for sess in sessions[:_DIGEST_RECENT_SESSIONS]:
                sid = getattr(sess, "id", "")
                if not sid:
                    continue
                title = (
                    (getattr(sess, "summary", "") or "").strip()
                    or getattr(sess, "active_agent", "")
                    or "(unnamed)"
                )
                status = getattr(sess, "status", None)
                status_val = getattr(status, "value", None) or str(status or "")
                body_lines.append(f"- [[{sid}]] · {status_val} · {title[:80]}")

        content = self._render_page(
            page_type="session_digest",
            page_id="recent-activity",
            body="\n".join(body_lines),
            generated_at=datetime.now(UTC).isoformat(),
            extra_fields={
                "window_sessions": _DIGEST_RECENT_SESSIONS,
                "window_log_entries": _DIGEST_LOG_TAIL,
                "session_total": len(sessions),
                "log_scanned": log_stats["scanned"],
                "log_tool_calls": log_stats["tool_calls"],
                "log_api_requests": log_stats["api_requests"],
                "log_errors": log_stats["errors"],
            },
        )
        digest_path.write_text(content, encoding="utf-8")
        return 1

    @staticmethod
    def _scan_deep_log_tail(limit: int) -> dict[str, Any]:
        """Tail-scan the JSONL deep log for aggregate stats.

        Returns a dict with totals and a per-tool breakdown. Returns
        zeros (and ``scanned=0``) when the log is missing or unreadable
        — never raises.
        """
        result: dict[str, Any] = {
            "scanned": 0,
            "tool_calls": 0,
            "api_requests": 0,
            "errors": 0,
            "by_tool": {},
        }
        log_path = Path.home() / ".obscura" / "logs" / "deep.jsonl"
        if not log_path.is_file():
            return result
        try:
            # Read full file then keep the last `limit` lines. Deep log
            # is rotated at 10MB per file so this is bounded.
            lines = log_path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except Exception:
            logger.debug("deep-log tail read failed", exc_info=True)
            return result
        tail = lines[-limit:] if len(lines) > limit else lines
        by_tool: dict[str, int] = {}
        for raw in tail:
            raw = raw.strip()
            if not raw:
                continue
            try:
                entry: Any = json.loads(raw)
            except Exception:
                logger.debug("deep-log entry parse failed", exc_info=True)
                continue
            if not isinstance(entry, dict):
                continue
            entry_dict = cast(dict[str, Any], entry)
            result["scanned"] = cast(int, result["scanned"]) + 1
            kind = str(entry_dict.get("type", ""))
            data: dict[str, Any] = entry_dict.get("data") or {}
            if kind == "tool_call":
                result["tool_calls"] = cast(int, result["tool_calls"]) + 1
                tool_name = str(data.get("tool", "") or "?")
                by_tool[tool_name] = by_tool.get(tool_name, 0) + 1
            elif kind == "api_request":
                result["api_requests"] = cast(int, result["api_requests"]) + 1
            elif kind == "error":
                result["errors"] = cast(int, result["errors"]) + 1
        result["by_tool"] = by_tool
        return result

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
            logger.debug("suppressed exception in _compute_hash", exc_info=True)
            return ""
        return h.hexdigest()

    @staticmethod
    def _parse_frontmatter(path: Path) -> tuple[dict[str, Any], str]:
        """Parse YAML frontmatter from a markdown file."""
        try:
            raw = path.read_text(encoding="utf-8")
        except Exception:
            logger.debug("suppressed exception in _parse_frontmatter", exc_info=True)
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
            logger.debug("suppressed exception in _parse_frontmatter", exc_info=True)
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
                logger.debug("suppressed exception in _load_hashes", exc_info=True)
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
        logger.debug("suppressed exception in notify_goal_changed", exc_info=True)


def notify_profile_changed() -> None:
    """Best-effort: re-export profile summary."""
    try:
        vs = _get_instance()
        if not vs.vault_dir.exists():
            return
        vs._export_profile_summary()  # pyright: ignore[reportPrivateUsage]
    except Exception:
        logger.debug("suppressed exception in notify_profile_changed", exc_info=True)
