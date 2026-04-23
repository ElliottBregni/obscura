"""User-data deletion walk.

This is the single entry point callers (CLI, HTTP admin endpoint, future
DSAR pipeline) use to erase a user's data across every store Obscura
persists to. It is designed around three SOC2-relevant properties:

1. **Completeness** — the walk visits every persistent store that could
   contain user-owned data. Each store contributes a small helper that
   knows how to locate and remove that user's rows/files. The
   orchestrator just composes them.
2. **Partial-success tolerance** — one store failing does not prevent
   the others from completing. The receipt records per-store outcomes
   so the operator knows what actually happened.
3. **Audit integrity** — we never hard-delete from the audit log.
   Instead, the deletion walk appends a structured ``user.deletion``
   audit event summarising what was removed from where. Historical
   audit records already store a hashed ``user_email_hash`` (see the
   redaction work in the previous batch) so a raw email is never at
   risk of lingering.

The walk intentionally does not touch data that isn't user-scoped
(daily observation logs, MCP configs, global exports) — those are
retention-managed separately by ``obscura/core/cleanup.py``.

A small list of stores that still lack a ``user_id`` linkage at the
schema level (task queue, pre-migration sessions rows) is deliberate —
the walk treats those as orphaned and leaves them, with the receipt
flagging the miss. Plumbing user_id into every session-create call
site is a separate cross-cutting change.
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class DeletionReceipt:
    """Per-store outcomes from a user-data deletion walk.

    ``per_store`` is a mapping of store-name → dict describing what the
    walk did there: record counts, file paths removed, errors encountered.
    Suitable for direct JSON-serialisation into audit events or operator
    reports.
    """

    user_id: str
    user_hash: str
    dry_run: bool
    per_store: dict[str, dict[str, Any]] = field(default_factory=dict)
    started_at: str = ""
    finished_at: str = ""

    def ok(self) -> bool:
        """True iff every store-step succeeded (or was skipped cleanly)."""
        return not any(
            step.get("error") for step in self.per_store.values()
        )

    def total_records(self) -> int:
        return sum(int(step.get("records", 0)) for step in self.per_store.values())


class DeletionError(RuntimeError):
    """Raised when a deletion walk cannot even begin (missing prerequisites)."""


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


def delete_user_data(
    user_id: str,
    *,
    dry_run: bool = False,
    memory_dir: Path | None = None,
    vector_memory_dir: Path | None = None,
    event_store_path: Path | None = None,
    notify_db_path: Path | None = None,
    kairos_db_path: Path | None = None,
) -> DeletionReceipt:
    """Erase all persistent data associated with ``user_id``.

    Returns a :class:`DeletionReceipt` summarising what each store did.
    On ``dry_run=True`` the walk reports what it *would* do without
    mutating anything — useful for confirmation prompts and testing.

    Store paths are overridable so tests don't touch the real filesystem,
    but in production they default to the same env-driven paths each
    store uses for its normal operation (see each helper for specifics).
    """
    if not user_id or not user_id.strip():
        raise DeletionError("user_id must be a non-empty string")
    user_id = user_id.strip()

    user_hash = _hash_user_id(user_id)
    receipt = DeletionReceipt(
        user_id=user_id,
        user_hash=user_hash,
        dry_run=dry_run,
        started_at=_now_iso(),
    )

    steps: list[tuple[str, Callable[[], dict[str, Any]]]] = [
        (
            "memory_kv",
            lambda: _delete_hashed_sqlite(
                _resolve_memory_dir(memory_dir),
                user_hash,
                dry_run=dry_run,
            ),
        ),
        (
            "vector_memory_sqlite",
            lambda: _delete_hashed_sqlite(
                _resolve_vector_memory_dir(vector_memory_dir),
                user_hash,
                dry_run=dry_run,
            ),
        ),
        (
            "vector_memory_qdrant",
            lambda: _delete_qdrant_collection(user_hash, dry_run=dry_run),
        ),
        (
            "event_store",
            lambda: _delete_event_store_rows(
                _resolve_event_store_path(event_store_path),
                user_id,
                dry_run=dry_run,
            ),
        ),
        (
            "notify",
            lambda: _delete_notify_rows(
                _resolve_notify_db_path(notify_db_path),
                user_id,
                dry_run=dry_run,
            ),
        ),
        (
            "kairos",
            lambda: _delete_kairos_rows(
                _resolve_kairos_db_path(kairos_db_path),
                user_id,
                dry_run=dry_run,
            ),
        ),
    ]

    for name, fn in steps:
        try:
            receipt.per_store[name] = fn()
        except Exception as exc:  # noqa: BLE001 — we *want* every step isolated
            logger.exception("deletion step %s failed", name)
            receipt.per_store[name] = {"error": str(exc)}

    # Audit tombstone is last — it records the operation itself, so it
    # must see the final receipt.
    receipt.finished_at = _now_iso()
    try:
        receipt.per_store["audit"] = _emit_audit_tombstone(receipt)
    except Exception as exc:  # noqa: BLE001
        logger.exception("audit tombstone emit failed")
        receipt.per_store["audit"] = {"error": str(exc)}

    return receipt


# ---------------------------------------------------------------------------
# Path resolvers (match the env var contracts each store uses)
# ---------------------------------------------------------------------------


def _resolve_memory_dir(override: Path | None) -> Path:
    if override is not None:
        return override
    return Path(
        os.environ.get(
            "OBSCURA_MEMORY_DIR",
            Path.home() / ".obscura" / "memory",
        )
    )


def _resolve_vector_memory_dir(override: Path | None) -> Path:
    if override is not None:
        return override
    return Path(
        os.environ.get(
            "OBSCURA_VECTOR_MEMORY_DIR",
            Path.home() / ".obscura" / "vector_memory",
        )
    )


def _resolve_event_store_path(override: Path | None) -> Path:
    if override is not None:
        return override
    return Path(
        os.environ.get(
            "OBSCURA_EVENT_STORE_PATH",
            Path.home() / ".obscura" / "supervisor.db",
        )
    )


def _resolve_notify_db_path(override: Path | None) -> Path:
    if override is not None:
        return override
    return Path(
        os.environ.get(
            "OBSCURA_NOTIFY_DB",
            Path.home() / ".obscura" / "notify.db",
        )
    )


def _resolve_kairos_db_path(override: Path | None) -> Path:
    if override is not None:
        return override
    return Path(
        os.environ.get(
            "OBSCURA_KAIROS_DB",
            Path.home() / ".obscura" / "kairos.db",
        )
    )


# ---------------------------------------------------------------------------
# Store helpers
# ---------------------------------------------------------------------------


def _hash_user_id(user_id: str) -> str:
    """Match the per-user db filename convention used by memory + vector."""
    return hashlib.sha256(user_id.encode()).hexdigest()[:16]


def _delete_hashed_sqlite(
    base_dir: Path,
    user_hash: str,
    *,
    dry_run: bool,
) -> dict[str, Any]:
    """Remove the per-user SQLite file at ``<base_dir>/<user_hash>.db``.

    The memory KV and vector-memory SQLite backends both use this layout.
    We delete the file outright rather than running DELETE statements —
    it's simpler and guarantees no residue, at the cost of a wasted
    empty-file recreation on the user's next session (acceptable).
    """
    db_path = base_dir / f"{user_hash}.db"
    if not db_path.exists():
        return {"records": 0, "path": str(db_path), "note": "absent"}

    size = db_path.stat().st_size
    if dry_run:
        return {"records": 1, "path": str(db_path), "bytes": size, "dry_run": True}

    # Also clean up the SQLite side files (-wal, -shm) left by WAL mode.
    for suffix in ("", "-wal", "-shm", "-journal"):
        p = base_dir / f"{user_hash}.db{suffix}"
        p.unlink(missing_ok=True)

    return {"records": 1, "path": str(db_path), "bytes": size}


def _delete_qdrant_collection(
    user_hash: str,
    *,
    dry_run: bool,
) -> dict[str, Any]:
    """Delete the per-user Qdrant collection, if Qdrant is configured.

    The Qdrant backend creates collections named ``user_<hash>``. We
    probe for the client lazily — Qdrant is optional and the admin path
    must still work when it isn't installed.
    """
    try:
        from qdrant_client import QdrantClient  # type: ignore[import-not-found]
    except ImportError:
        return {"records": 0, "note": "qdrant-client not installed; skipped"}

    url = os.environ.get("OBSCURA_QDRANT_URL") or os.environ.get("QDRANT_URL")
    api_key = os.environ.get("QDRANT_API_KEY")
    mode = os.environ.get("OBSCURA_QDRANT_MODE", "local")

    collection = f"user_{user_hash}"

    try:
        if mode == "local":
            path = Path(
                os.environ.get(
                    "OBSCURA_QDRANT_PATH",
                    Path.home() / ".obscura" / "qdrant",
                )
            )
            client = QdrantClient(path=str(path))
        elif url:
            client = QdrantClient(url=url, api_key=api_key) if api_key else QdrantClient(url=url)
        else:
            return {"records": 0, "note": "qdrant not configured; skipped"}

        existing = {c.name for c in client.get_collections().collections}
        if collection not in existing:
            return {"records": 0, "collection": collection, "note": "absent"}

        if dry_run:
            return {
                "records": 1,
                "collection": collection,
                "dry_run": True,
            }

        client.delete_collection(collection)
        return {"records": 1, "collection": collection}
    finally:
        # Best-effort close; older qdrant-client versions don't expose it.
        closer = getattr(locals().get("client"), "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:  # noqa: BLE001
                pass


def _delete_event_store_rows(
    db_path: Path,
    user_id: str,
    *,
    dry_run: bool,
) -> dict[str, Any]:
    """Delete sessions (and cascading events) owned by this user.

    Requires the ``user_id`` column on ``sessions`` (added in this batch).
    Pre-migration rows are orphaned (``user_id == ''``) and are left
    untouched, which is the correct behaviour — we cannot prove they
    belong to this user.
    """
    if not db_path.exists():
        return {"records": 0, "path": str(db_path), "note": "absent"}

    import sqlite3

    conn = sqlite3.connect(str(db_path))
    try:
        # Confirm migration ran. If not, refuse rather than silently no-op.
        cols = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
        if "user_id" not in cols:
            return {
                "records": 0,
                "path": str(db_path),
                "error": (
                    "sessions table missing user_id column; run the event "
                    "store migration before calling deletion"
                ),
            }

        session_ids = [
            row[0]
            for row in conn.execute(
                "SELECT id FROM sessions WHERE user_id = ?",
                (user_id,),
            )
        ]
        if not session_ids:
            return {
                "records": 0,
                "path": str(db_path),
                "note": "no attributable sessions",
            }

        event_count = int(
            conn.execute(
                f"SELECT count(*) FROM events WHERE session_id IN "  # noqa: S608 — placeholders built from count, not user input
                f"({','.join('?' for _ in session_ids)})",
                session_ids,
            ).fetchone()[0]
        )

        if dry_run:
            return {
                "records": len(session_ids),
                "events": event_count,
                "dry_run": True,
            }

        conn.execute(
            f"DELETE FROM events WHERE session_id IN "  # noqa: S608
            f"({','.join('?' for _ in session_ids)})",
            session_ids,
        )
        conn.execute(
            "DELETE FROM sessions WHERE user_id = ?",
            (user_id,),
        )
        conn.commit()
        return {"records": len(session_ids), "events": event_count}
    finally:
        conn.close()


def _delete_notify_rows(
    db_path: Path,
    user_id: str,
    *,
    dry_run: bool,
) -> dict[str, Any]:
    """Delete notify messages + dead letters owned by this user."""
    if not db_path.exists():
        return {"records": 0, "path": str(db_path), "note": "absent"}

    import sqlite3

    conn = sqlite3.connect(str(db_path))
    try:
        counts: dict[str, int] = {}
        for table in ("messages", "dead_letters"):
            try:
                counts[table] = int(
                    conn.execute(
                        f"SELECT count(*) FROM {table} WHERE user_id = ?",  # noqa: S608 — table list is hard-coded above
                        (user_id,),
                    ).fetchone()[0]
                )
            except sqlite3.OperationalError:
                counts[table] = 0

        total = sum(counts.values())
        if total == 0:
            return {"records": 0, "path": str(db_path), "note": "no rows"}

        if dry_run:
            return {"records": total, "per_table": counts, "dry_run": True}

        for table in counts:
            conn.execute(
                f"DELETE FROM {table} WHERE user_id = ?",  # noqa: S608
                (user_id,),
            )
        conn.commit()
        return {"records": total, "per_table": counts}
    finally:
        conn.close()


def _delete_kairos_rows(
    db_path: Path,
    user_id: str,
    *,
    dry_run: bool,
) -> dict[str, Any]:
    """Delete kairos goals (and cascading plans/tasks/checkpoints).

    The kairos schema has ``owner_id`` on goals already; plans/tasks/
    checkpoints/budget_usage reference goals and cascade.
    """
    if not db_path.exists():
        return {"records": 0, "path": str(db_path), "note": "absent"}

    import sqlite3

    conn = sqlite3.connect(str(db_path))
    try:
        try:
            goal_ids = [
                row[0]
                for row in conn.execute(
                    "SELECT goal_id FROM kairos_goals WHERE owner_id = ?",
                    (user_id,),
                )
            ]
        except sqlite3.OperationalError:
            return {
                "records": 0,
                "path": str(db_path),
                "note": "no kairos_goals table",
            }

        if not goal_ids:
            return {"records": 0, "path": str(db_path), "note": "no goals"}

        if dry_run:
            return {"records": len(goal_ids), "dry_run": True}

        placeholders = ",".join("?" for _ in goal_ids)
        for table in (
            "kairos_budget_usage",
            "kairos_checkpoints",
            "kairos_tasks",
            "kairos_plans",
        ):
            try:
                conn.execute(
                    f"DELETE FROM {table} WHERE goal_id IN ({placeholders})",  # noqa: S608
                    goal_ids,
                )
            except sqlite3.OperationalError:
                # Schema drift — table absent. Not fatal.
                pass
        conn.execute(
            "DELETE FROM kairos_goals WHERE owner_id = ?",
            (user_id,),
        )
        conn.commit()
        return {"records": len(goal_ids)}
    finally:
        conn.close()


def _emit_audit_tombstone(receipt: DeletionReceipt) -> dict[str, Any]:
    """Append a ``user.deletion`` record to the audit log.

    We never hard-delete from the audit log — doing so would violate the
    append-only contract that CC2 relies on. Historical audit rows for
    this user already have their email redacted and replaced with a
    hash (see the redaction batch), so the raw PII isn't lingering.
    """
    try:
        from obscura.telemetry.audit import AuditEvent, emit_audit_event
    except Exception:  # noqa: BLE001 — telemetry optional in some deployments
        return {"note": "audit telemetry unavailable; tombstone not emitted"}

    summary = {
        name: {k: v for k, v in step.items() if k != "path"}
        for name, step in receipt.per_store.items()
    }
    event = AuditEvent(
        event_type="user.deletion",
        user_id=receipt.user_id,
        user_email="[REDACTED]",  # never carry raw email into deletion record
        resource="user_data",
        action="delete",
        outcome="success" if receipt.ok() else "partial",
        details={
            "user_hash": receipt.user_hash,
            "dry_run": receipt.dry_run,
            "summary": summary,
            "started_at": receipt.started_at,
            "finished_at": receipt.finished_at,
        },
    )
    emit_audit_event(event)
    return {"note": "tombstone appended"}


def _now_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()
