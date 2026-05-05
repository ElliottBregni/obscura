"""SQLite implementation of the :class:`TaskRepo` Protocol.

Per-call connections (WAL mode + busy-timeout) — no long-lived state.
Schema additions are idempotent ``ALTER TABLE`` calls; SQLite's "column
already exists" error is suppressed at debug log level.

Migrated from ``obscura.core.task_queue`` as part of Phase 3b. Behaviour
byte-for-byte identical; class renamed (``TaskQueue`` → ``SqliteTaskRepo``)
and SQL extracted into module-level ``_QUERIES`` where it doesn't depend
on dynamic WHERE clauses. Queries with optional filters keep their
inline construction since the conditional-SQL pattern is awkward to
template.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from obscura.core.enums.lifecycle import TaskQueueStatus

logger = logging.getLogger(__name__)


# How long (seconds) before a claimed-but-unheartbeated task is reclaimed.
DEFAULT_CLAIM_TIMEOUT = 120.0


_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id        TEXT PRIMARY KEY,
    subject        TEXT NOT NULL,
    description    TEXT NOT NULL DEFAULT '',
    status         TEXT NOT NULL DEFAULT 'pending',
    owner          TEXT NOT NULL DEFAULT '',
    active_form    TEXT NOT NULL DEFAULT '',
    metadata       TEXT NOT NULL DEFAULT '{}',
    blocks         TEXT NOT NULL DEFAULT '[]',
    blocked_by     TEXT NOT NULL DEFAULT '[]',
    created_at     REAL NOT NULL,
    updated_at     REAL NOT NULL
)
"""


_MIGRATIONS: tuple[tuple[str, str], ...] = (
    ("priority", "INTEGER DEFAULT 50"),
    ("claimed_by", "TEXT DEFAULT ''"),
    ("claimed_at", "REAL DEFAULT 0"),
    ("goal_id", "TEXT DEFAULT ''"),
    ("run_after", "REAL DEFAULT 0"),
    ("max_retries", "INTEGER DEFAULT 3"),
    ("retry_count", "INTEGER DEFAULT 0"),
    ("last_heartbeat", "REAL DEFAULT 0"),
    ("error", "TEXT DEFAULT ''"),
    ("output", "TEXT DEFAULT ''"),
    ("project_root", "TEXT DEFAULT ''"),
)


_INDEX_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_tasks_queue"
    " ON tasks (status, priority, run_after, claimed_by)"
)


_QUERIES = {
    "insert": (
        "INSERT INTO tasks "
        "(task_id, subject, description, status,"
        " priority, goal_id, blocked_by, run_after,"
        " max_retries, retry_count, metadata, project_root,"
        " created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,0,?,?,?,?)"
    ),
    "claim": (
        "UPDATE tasks SET claimed_by = ?, claimed_at = ?, last_heartbeat = ?,"
        " updated_at = ? WHERE task_id = ? AND status = ?"
        " AND (claimed_by = '' OR claimed_at < ?)"
    ),
    "release": (
        "UPDATE tasks SET claimed_by = '', claimed_at = 0, updated_at = ?"
        " WHERE task_id = ? AND claimed_by = ?"
    ),
    "heartbeat": (
        "UPDATE tasks SET last_heartbeat = ?, claimed_at = ?, updated_at = ?"
        " WHERE task_id = ? AND claimed_by = ?"
    ),
    "complete": (
        "UPDATE tasks SET status = ?, output = ?, claimed_by = '',"
        " updated_at = ? WHERE task_id = ?"
    ),
    "fail_retry": (
        "UPDATE tasks SET status = ?, error = ?, claimed_by = '',"
        " claimed_at = 0, retry_count = ?, run_after = ?, updated_at = ?"
        " WHERE task_id = ?"
    ),
    "fail_permanent": (
        "UPDATE tasks SET status = ?, error = ?, claimed_by = '',"
        " updated_at = ? WHERE task_id = ?"
    ),
    "get_retry_state": ("SELECT retry_count, max_retries FROM tasks WHERE task_id = ?"),
    "get_dep_status": "SELECT status FROM tasks WHERE task_id = ?",
    "get": "SELECT * FROM tasks WHERE task_id = ?",
    "reclaim_stale": (
        "UPDATE tasks SET claimed_by = '', claimed_at = 0, updated_at = ?"
        " WHERE status = ? AND claimed_by != '' AND claimed_at < ?"
    ),
}


def _db_path() -> Path:
    return Path.home() / ".obscura" / "tasks.db"


def _open() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the base table and add queue-specific columns idempotently."""
    conn.execute(_SCHEMA)
    for col, definition in _MIGRATIONS:
        _add_col(conn, col, definition)
    conn.execute(_INDEX_DDL)
    conn.commit()


def _add_col(conn: sqlite3.Connection, col: str, definition: str) -> None:
    try:
        conn.execute(f"ALTER TABLE tasks ADD COLUMN {col} {definition}")
        conn.commit()
    except sqlite3.OperationalError:
        logger.debug("suppressed exception in _add_col", exc_info=True)


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    for key in ("metadata", "blocks", "blocked_by"):
        if key in d and isinstance(d[key], str):
            try:
                d[key] = json.loads(d[key])
            except Exception:
                logger.debug("suppressed exception in _row_to_dict", exc_info=True)
                d[key] = {} if key == "metadata" else []
    return d


class SqliteTaskRepo:
    """SQLite implementation of :class:`TaskRepo`.

    Thread-safe: each operation opens a fresh connection. WAL mode plus
    SQLite's ``busy_timeout`` PRAGMA handles concurrent readers + a
    single writer. Per-call connections trade a small open/close cost
    for simplicity.
    """

    def __init__(self, claim_timeout: float = DEFAULT_CLAIM_TIMEOUT) -> None:
        self._claim_timeout = claim_timeout

    # ------------------------------------------------------------------
    # Enqueue
    # ------------------------------------------------------------------

    def enqueue(
        self,
        subject: str,
        *,
        description: str = "",
        priority: int = 50,
        goal_id: str = "",
        blocked_by: list[str] | None = None,
        run_after: float = 0.0,
        max_retries: int = 3,
        metadata: dict[str, Any] | None = None,
        project_root: str = "",
    ) -> str:
        task_id = uuid.uuid4().hex[:12]
        now = time.time()
        conn = _open()
        try:
            conn.execute(
                _QUERIES["insert"],
                (
                    task_id,
                    subject,
                    description,
                    TaskQueueStatus.PENDING.value,
                    priority,
                    goal_id,
                    json.dumps(blocked_by or []),
                    run_after,
                    max_retries,
                    json.dumps(metadata or {}),
                    project_root,
                    now,
                    now,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return task_id

    # ------------------------------------------------------------------
    # Dequeue
    # ------------------------------------------------------------------

    def next_ready(
        self,
        *,
        worker_id: str = "",
        project_root: str | None = None,
    ) -> dict[str, Any] | None:
        # ``worker_id`` is reserved for future per-worker affinity; today
        # any worker can claim any ready task. Kept in the signature so
        # callers don't break when we wire affinity in later.
        del worker_id
        now = time.time()
        stale_threshold = now - self._claim_timeout
        conn = _open()
        try:
            sql = (
                "SELECT * FROM tasks WHERE status = ? AND run_after <= ?"
                " AND (claimed_by = '' OR claimed_at < ?)"
            )
            params: list[Any] = [
                TaskQueueStatus.PENDING.value,
                now,
                stale_threshold,
            ]
            if project_root is not None:
                sql += " AND project_root = ?"
                params.append(project_root)
            sql += " ORDER BY priority ASC, created_at ASC LIMIT 50"
            rows = conn.execute(sql, params).fetchall()
            for row in rows:
                task = _row_to_dict(row)
                if self._deps_satisfied(conn, task):
                    return task
            return None
        finally:
            conn.close()

    def _deps_satisfied(
        self,
        conn: sqlite3.Connection,
        task: dict[str, Any],
    ) -> bool:
        deps: list[str] = task.get("blocked_by") or []
        if not deps:
            return True
        for dep_id in deps:
            row = conn.execute(_QUERIES["get_dep_status"], (dep_id,)).fetchone()
            if row is None or row["status"] != TaskQueueStatus.COMPLETED.value:
                return False
        return True

    # ------------------------------------------------------------------
    # Claim / Release
    # ------------------------------------------------------------------

    def claim(self, task_id: str, worker_id: str) -> bool:
        now = time.time()
        stale_threshold = now - self._claim_timeout
        conn = _open()
        try:
            cursor = conn.execute(
                _QUERIES["claim"],
                (
                    worker_id,
                    now,
                    now,
                    now,
                    task_id,
                    TaskQueueStatus.PENDING.value,
                    stale_threshold,
                ),
            )
            conn.commit()
            return cursor.rowcount == 1
        finally:
            conn.close()

    def release(self, task_id: str, worker_id: str) -> bool:
        now = time.time()
        conn = _open()
        try:
            cursor = conn.execute(
                _QUERIES["release"],
                (now, task_id, worker_id),
            )
            conn.commit()
            return cursor.rowcount == 1
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Heartbeat
    # ------------------------------------------------------------------

    def heartbeat(self, task_id: str, worker_id: str) -> bool:
        now = time.time()
        conn = _open()
        try:
            cursor = conn.execute(
                _QUERIES["heartbeat"],
                (now, now, now, task_id, worker_id),
            )
            conn.commit()
            return cursor.rowcount == 1
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Completion / Failure
    # ------------------------------------------------------------------

    def complete(self, task_id: str, *, output: str = "") -> bool:
        now = time.time()
        conn = _open()
        try:
            cursor = conn.execute(
                _QUERIES["complete"],
                (TaskQueueStatus.COMPLETED.value, output, now, task_id),
            )
            conn.commit()
            return cursor.rowcount == 1
        finally:
            conn.close()

    def fail(self, task_id: str, error: str, *, retry: bool = True) -> bool:
        now = time.time()
        conn = _open()
        try:
            row = conn.execute(
                _QUERIES["get_retry_state"],
                (task_id,),
            ).fetchone()
            if row is None:
                return False

            retry_count: int = row["retry_count"]
            max_retries: int = row["max_retries"]
            can_retry = retry and retry_count < max_retries

            if can_retry:
                new_count = retry_count + 1
                # Exponential backoff: 30s, 60s, 120s, …
                backoff = 30.0 * (2 ** (new_count - 1))
                conn.execute(
                    _QUERIES["fail_retry"],
                    (
                        TaskQueueStatus.PENDING.value,
                        error,
                        new_count,
                        now + backoff,
                        now,
                        task_id,
                    ),
                )
            else:
                conn.execute(
                    _QUERIES["fail_permanent"],
                    (TaskQueueStatus.FAILED.value, error, now, task_id),
                )
            conn.commit()
            return True
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Inspection
    # ------------------------------------------------------------------

    def queue_depth(
        self,
        *,
        status: str | TaskQueueStatus = TaskQueueStatus.PENDING,
        worker_id: str = "",
        project_root: str | None = None,
    ) -> dict[str, int]:
        status_value = status.value if isinstance(status, TaskQueueStatus) else status
        conn = _open()
        try:
            sql = "SELECT priority, COUNT(*) AS cnt FROM tasks WHERE status = ?"
            params: list[Any] = [status_value]
            if worker_id:
                sql += " AND claimed_by = ?"
                params.append(worker_id)
            if project_root is not None:
                sql += " AND project_root = ?"
                params.append(project_root)
            sql += " GROUP BY priority"
            rows = conn.execute(sql, params).fetchall()
            return {str(r["priority"]): r["cnt"] for r in rows}
        finally:
            conn.close()

    def get(self, task_id: str) -> dict[str, Any] | None:
        conn = _open()
        try:
            row = conn.execute(_QUERIES["get"], (task_id,)).fetchone()
            return _row_to_dict(row) if row else None
        finally:
            conn.close()

    def list_claimed(
        self,
        worker_id: str,
        *,
        project_root: str | None = None,
    ) -> list[dict[str, Any]]:
        conn = _open()
        try:
            sql = "SELECT * FROM tasks WHERE claimed_by = ?"
            params: list[Any] = [worker_id]
            if project_root is not None:
                sql += " AND project_root = ?"
                params.append(project_root)
            sql += " ORDER BY claimed_at ASC"
            rows = conn.execute(sql, params).fetchall()
            return [_row_to_dict(r) for r in rows]
        finally:
            conn.close()

    def reclaim_stale(self) -> int:
        now = time.time()
        stale_threshold = now - self._claim_timeout
        conn = _open()
        try:
            cursor = conn.execute(
                _QUERIES["reclaim_stale"],
                (now, TaskQueueStatus.PENDING.value, stale_threshold),
            )
            conn.commit()
            return cursor.rowcount
        finally:
            conn.close()
