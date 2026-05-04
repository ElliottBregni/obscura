"""obscura.core.task_queue — SQLite-backed work queue with claim semantics.

Sits on top of the existing ``~/.obscura/tasks.db`` tasks table and adds
proper queue behaviour:

- **Priority ordering** — tasks are pulled highest-priority-first.
- **Atomic claiming** — prevents two workers from picking the same task.
- **Heartbeat / stall detection** — workers must ping periodically; stalled
  claims are automatically reclaimed.
- **Dependency gating** — tasks whose ``blocked_by`` deps are not yet
  completed stay invisible to ``next_ready()``.
- **Retry on failure** — failed tasks are requeued up to ``max_retries``
  times before being marked ``failed`` permanently.
- **Scheduled tasks** — ``run_after`` keeps a task invisible until a future
  unix timestamp.

Schema additions (applied via ``_ensure_schema()``)::

    priority       INTEGER DEFAULT 50   -- 0 = critical, 100 = lowest
    claimed_by     TEXT    DEFAULT ''   -- worker_id that holds the claim
    claimed_at     REAL    DEFAULT 0    -- unix ts of last claim
    goal_id        TEXT    DEFAULT ''   -- parent goal (optional)
    run_after      REAL    DEFAULT 0    -- earliest time to dequeue
    max_retries    INTEGER DEFAULT 3
    retry_count    INTEGER DEFAULT 0
    last_heartbeat REAL    DEFAULT 0
    error          TEXT    DEFAULT ''
    output         TEXT    DEFAULT ''

Usage::

    q = TaskQueue()
    task_id = q.enqueue("Run benchmarks", description="pytest -v", priority=10)
    task = q.next_ready(worker_id="kairos-worker")
    if task and q.claim(task["task_id"], "kairos-worker"):
        try:
            # ... do work ...
            q.complete(task["task_id"], output="all tests passed")
        except Exception as exc:
            q.fail(task["task_id"], str(exc))
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any
import logging

from obscura.core.enums.lifecycle import TaskQueueStatus as TaskQueueStatus

logger = logging.getLogger(__name__)


# How long (seconds) before a claimed-but-unheartbeated task is reclaimed.
_CLAIM_TIMEOUT = 120.0


def _db_path() -> Path:
    return Path.home() / ".obscura" / "tasks.db"


def _open() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # WAL mode for concurrent readers + writer.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Create base table (if missing) and add queue-specific columns."""
    conn.execute("""
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
    """)

    # Queue-specific columns — ALTER TABLE ADD COLUMN is idempotent for
    # "column already exists" errors in SQLite; we just suppress them.
    _add_col(conn, "priority", "INTEGER DEFAULT 50")
    _add_col(conn, "claimed_by", "TEXT DEFAULT ''")
    _add_col(conn, "claimed_at", "REAL DEFAULT 0")
    _add_col(conn, "goal_id", "TEXT DEFAULT ''")
    _add_col(conn, "run_after", "REAL DEFAULT 0")
    _add_col(conn, "max_retries", "INTEGER DEFAULT 3")
    _add_col(conn, "retry_count", "INTEGER DEFAULT 0")
    _add_col(conn, "last_heartbeat", "REAL DEFAULT 0")
    _add_col(conn, "error", "TEXT DEFAULT ''")
    _add_col(conn, "output", "TEXT DEFAULT ''")
    _add_col(conn, "project_root", "TEXT DEFAULT ''")

    # Index to make next_ready() fast.
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_tasks_queue
        ON tasks (status, priority, run_after, claimed_by)
    """)
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


class TaskQueue:
    """Work queue layered on top of ``~/.obscura/tasks.db``.

    Thread-safe for concurrent readers; writers use SQLite WAL + busy-timeout.
    Instantiate a fresh ``TaskQueue()`` per operation — connections are not
    kept open between calls.
    """

    def __init__(self, claim_timeout: float = _CLAIM_TIMEOUT) -> None:
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
        """Create a new task in the queue and return its task_id.

        *priority* follows the same convention as the goal board:
        0 = critical, 25 = high, 50 = medium (default), 75 = low, 100 = lowest.
        """
        task_id = uuid.uuid4().hex[:12]
        now = time.time()
        conn = _open()
        try:
            conn.execute(
                """INSERT INTO tasks
                   (task_id, subject, description, status,
                    priority, goal_id, blocked_by, run_after,
                    max_retries, retry_count, metadata,
                    project_root,
                    created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,0,?,?,?,?)""",
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
        self, *, worker_id: str = "", project_root: str | None = None
    ) -> dict[str, Any] | None:
        """Return the highest-priority task that is ready to be worked.

        A task is *ready* when:
        - ``status = 'pending'``
        - ``run_after <= now``
        - not claimed by another live worker
          (claim_timeout seconds without a heartbeat releases the claim)
        - all ``blocked_by`` task_ids have ``status = 'completed'``

        Returns a task dict or ``None`` if the queue is empty / all blocked.
        """
        now = time.time()
        stale_threshold = now - self._claim_timeout
        conn = _open()
        try:
            where = """SELECT * FROM tasks
                   WHERE status = ?
                     AND run_after <= ?
                     AND (
                           claimed_by = ''
                           OR claimed_at < ?
                     )"""
            params: list[Any] = [
                TaskQueueStatus.PENDING.value,
                now,
                stale_threshold,
            ]
            if project_root is not None:
                where += "\n                     AND project_root = ?"
                params.append(project_root)
            where += "\n                   ORDER BY priority ASC, created_at ASC"
            where += "\n                   LIMIT 50"
            rows = conn.execute(where, params).fetchall()

            for row in rows:
                task = _row_to_dict(row)
                if self._deps_satisfied(conn, task):
                    return task
            return None
        finally:
            conn.close()

    def _deps_satisfied(self, conn: sqlite3.Connection, task: dict[str, Any]) -> bool:
        """Return True if all blocked_by deps are completed."""
        deps: list[str] = task.get("blocked_by") or []
        if not deps:
            return True
        for dep_id in deps:
            row = conn.execute(
                "SELECT status FROM tasks WHERE task_id = ?", (dep_id,)
            ).fetchone()
            if row is None or row["status"] != TaskQueueStatus.COMPLETED.value:
                return False
        return True

    # ------------------------------------------------------------------
    # Claim / Release
    # ------------------------------------------------------------------

    def claim(self, task_id: str, worker_id: str) -> bool:
        """Atomically claim *task_id* for *worker_id*.

        Returns ``True`` on success, ``False`` if already claimed by someone
        else or not found.
        """
        now = time.time()
        stale_threshold = now - self._claim_timeout
        conn = _open()
        try:
            cursor = conn.execute(
                """UPDATE tasks
                   SET claimed_by = ?, claimed_at = ?, last_heartbeat = ?, updated_at = ?
                   WHERE task_id = ?
                     AND status = ?
                     AND (claimed_by = '' OR claimed_at < ?)""",
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
        """Release a claim without completing — task goes back to unclaimed."""
        now = time.time()
        conn = _open()
        try:
            cursor = conn.execute(
                """UPDATE tasks
                   SET claimed_by = '', claimed_at = 0, updated_at = ?
                   WHERE task_id = ? AND claimed_by = ?""",
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
        """Touch last_heartbeat to prove the worker is still alive.

        Must be called at least once per ``claim_timeout`` seconds or the
        claim will be reclaimed by the next ``next_ready()`` call.
        """
        now = time.time()
        conn = _open()
        try:
            cursor = conn.execute(
                """UPDATE tasks
                   SET last_heartbeat = ?, claimed_at = ?, updated_at = ?
                   WHERE task_id = ? AND claimed_by = ?""",
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
        """Mark a task completed and store its output."""
        now = time.time()
        conn = _open()
        try:
            cursor = conn.execute(
                """UPDATE tasks
                   SET status = ?, output = ?,
                       claimed_by = '', updated_at = ?
                   WHERE task_id = ?""",
                (TaskQueueStatus.COMPLETED.value, output, now, task_id),
            )
            conn.commit()
            return cursor.rowcount == 1
        finally:
            conn.close()

    def fail(self, task_id: str, error: str, *, retry: bool = True) -> bool:
        """Mark a task failed.

        If *retry* is True and ``retry_count < max_retries``, the task is
        requeued as ``pending`` with an incremented retry count and an
        exponential back-off applied to ``run_after``.  Otherwise it is
        permanently set to ``failed``.

        Returns True if the row was updated.
        """
        now = time.time()
        conn = _open()
        try:
            row = conn.execute(
                "SELECT retry_count, max_retries FROM tasks WHERE task_id = ?",
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
                    """UPDATE tasks
                       SET status = ?, error = ?,
                           claimed_by = '', claimed_at = 0,
                           retry_count = ?, run_after = ?, updated_at = ?
                       WHERE task_id = ?""",
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
                    """UPDATE tasks
                       SET status = ?, error = ?,
                           claimed_by = '', updated_at = ?
                       WHERE task_id = ?""",
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
        """Return counts by priority bucket for quick diagnostics."""
        status_value = (
            status.value if isinstance(status, TaskQueueStatus) else status
        )
        conn = _open()
        try:
            where = "WHERE status = ?"
            params_q: list[Any] = [status_value]
            if worker_id:
                where += " AND claimed_by = ?"
                params_q.append(worker_id)
            if project_root is not None:
                where += " AND project_root = ?"
                params_q.append(project_root)
            rows = conn.execute(
                f"SELECT priority, COUNT(*) as cnt FROM tasks {where} GROUP BY priority",
                params_q,
            ).fetchall()
            return {str(r["priority"]): r["cnt"] for r in rows}
        finally:
            conn.close()

    def get(self, task_id: str) -> dict[str, Any] | None:
        """Fetch a single task by ID."""
        conn = _open()
        try:
            row = conn.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            return _row_to_dict(row) if row else None
        finally:
            conn.close()

    def list_claimed(
        self, worker_id: str, *, project_root: str | None = None
    ) -> list[dict[str, Any]]:
        """Return all tasks currently claimed by *worker_id*."""
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
        """Release all claims older than claim_timeout. Returns count released."""
        now = time.time()
        stale_threshold = now - self._claim_timeout
        conn = _open()
        try:
            cursor = conn.execute(
                """UPDATE tasks
                   SET claimed_by = '', claimed_at = 0, updated_at = ?
                   WHERE status = ?
                     AND claimed_by != ''
                     AND claimed_at < ?""",
                (now, TaskQueueStatus.PENDING.value, stale_threshold),
            )
            conn.commit()
            return cursor.rowcount
        finally:
            conn.close()
