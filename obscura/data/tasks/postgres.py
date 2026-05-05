"""PostgreSQL implementation of the :class:`TaskRepo` Protocol.

Better concurrency story than SQLite: claim semantics use
``UPDATE … RETURNING`` with ``FOR UPDATE SKIP LOCKED`` for true
fan-out across many workers without claim-collision races.

Connections come from :func:`obscura.data.engine.postgres_connection`.
Schema and queries mirror the SQLite shape so callers see no
behavioural difference beyond throughput.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any

from obscura.core.enums.lifecycle import TaskQueueStatus
from obscura.data.engine import postgres_connection

logger = logging.getLogger(__name__)


DEFAULT_CLAIM_TIMEOUT = 120.0


_SCHEMA = """
CREATE TABLE IF NOT EXISTS obscura_tasks (
    task_id        TEXT PRIMARY KEY,
    subject        TEXT NOT NULL,
    description    TEXT NOT NULL DEFAULT '',
    status         TEXT NOT NULL DEFAULT 'pending',
    owner          TEXT NOT NULL DEFAULT '',
    active_form    TEXT NOT NULL DEFAULT '',
    metadata       JSONB NOT NULL DEFAULT '{}'::jsonb,
    blocks         JSONB NOT NULL DEFAULT '[]'::jsonb,
    blocked_by     JSONB NOT NULL DEFAULT '[]'::jsonb,
    priority       INTEGER NOT NULL DEFAULT 50,
    claimed_by     TEXT NOT NULL DEFAULT '',
    claimed_at     DOUBLE PRECISION NOT NULL DEFAULT 0,
    goal_id        TEXT NOT NULL DEFAULT '',
    run_after      DOUBLE PRECISION NOT NULL DEFAULT 0,
    max_retries    INTEGER NOT NULL DEFAULT 3,
    retry_count    INTEGER NOT NULL DEFAULT 0,
    last_heartbeat DOUBLE PRECISION NOT NULL DEFAULT 0,
    error          TEXT NOT NULL DEFAULT '',
    output         TEXT NOT NULL DEFAULT '',
    project_root   TEXT NOT NULL DEFAULT '',
    created_at     DOUBLE PRECISION NOT NULL,
    updated_at     DOUBLE PRECISION NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_obscura_tasks_queue
    ON obscura_tasks (status, priority, run_after, claimed_by);
"""


_QUERIES = {
    "insert": (
        "INSERT INTO obscura_tasks "
        "(task_id, subject, description, status, priority, goal_id, "
        " blocked_by, run_after, max_retries, retry_count, metadata, "
        " project_root, created_at, updated_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, 0, %s::jsonb, "
        " %s, %s, %s)"
    ),
    "claim": (
        "UPDATE obscura_tasks SET claimed_by = %s, claimed_at = %s, "
        "last_heartbeat = %s, updated_at = %s "
        "WHERE task_id = %s AND status = %s "
        " AND (claimed_by = '' OR claimed_at < %s)"
    ),
    "release": (
        "UPDATE obscura_tasks SET claimed_by = '', claimed_at = 0, "
        "updated_at = %s WHERE task_id = %s AND claimed_by = %s"
    ),
    "heartbeat": (
        "UPDATE obscura_tasks SET last_heartbeat = %s, claimed_at = %s, "
        "updated_at = %s WHERE task_id = %s AND claimed_by = %s"
    ),
    "complete": (
        "UPDATE obscura_tasks SET status = %s, output = %s, claimed_by = '', "
        "updated_at = %s WHERE task_id = %s"
    ),
    "fail_retry": (
        "UPDATE obscura_tasks SET status = %s, error = %s, claimed_by = '', "
        "claimed_at = 0, retry_count = %s, run_after = %s, updated_at = %s "
        "WHERE task_id = %s"
    ),
    "fail_permanent": (
        "UPDATE obscura_tasks SET status = %s, error = %s, claimed_by = '', "
        "updated_at = %s WHERE task_id = %s"
    ),
    "get_retry_state": (
        "SELECT retry_count, max_retries FROM obscura_tasks WHERE task_id = %s"
    ),
    "get_dep_status": "SELECT status FROM obscura_tasks WHERE task_id = %s",
    "get": "SELECT * FROM obscura_tasks WHERE task_id = %s",
    "reclaim_stale": (
        "UPDATE obscura_tasks SET claimed_by = '', claimed_at = 0, "
        "updated_at = %s WHERE status = %s AND claimed_by != '' "
        "AND claimed_at < %s"
    ),
}


def _row_to_dict(row: Any) -> dict[str, Any]:  # noqa: ANN401  # psycopg2 RealDictRow
    """Convert a psycopg2 RealDictRow to a plain dict.

    JSONB columns come back already-decoded by psycopg2.
    """
    d = dict(row)
    for key in ("metadata", "blocks", "blocked_by"):
        # psycopg2 returns JSONB as native dict/list; only normalise if a
        # backend hands us the raw text (shouldn't happen with
        # RealDictCursor, but defensive).
        v = d.get(key)
        if isinstance(v, str):
            try:
                d[key] = json.loads(v)
            except (json.JSONDecodeError, TypeError):
                logger.debug("invalid JSON for %s: %r", key, v, exc_info=True)
                d[key] = {} if key == "metadata" else []
    return d


class PostgresTaskRepo:
    """Postgres implementation of :class:`TaskRepo`."""

    _schema_initialized = False

    def __init__(self, claim_timeout: float = DEFAULT_CLAIM_TIMEOUT) -> None:
        self._claim_timeout = claim_timeout
        if PostgresTaskRepo._schema_initialized:
            return
        with postgres_connection() as conn:
            try:
                with conn.cursor() as cur:
                    cur.execute(_SCHEMA)
                conn.commit()
                PostgresTaskRepo._schema_initialized = True
            except Exception:
                conn.rollback()
                raise

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
        with postgres_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
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
        return task_id

    # ------------------------------------------------------------------
    # Dequeue (uses FOR UPDATE SKIP LOCKED for true atomic claim)
    # ------------------------------------------------------------------

    def next_ready(
        self,
        *,
        worker_id: str = "",
        project_root: str | None = None,
    ) -> dict[str, Any] | None:
        del worker_id  # reserved for affinity
        now = time.time()
        stale_threshold = now - self._claim_timeout
        sql = (
            "SELECT * FROM obscura_tasks WHERE status = %s AND run_after <= %s "
            "AND (claimed_by = '' OR claimed_at < %s)"
        )
        params: list[Any] = [
            TaskQueueStatus.PENDING.value,
            now,
            stale_threshold,
        ]
        if project_root is not None:
            sql += " AND project_root = %s"
            params.append(project_root)
        sql += " ORDER BY priority ASC, created_at ASC LIMIT 50"
        with postgres_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
                for row in rows:
                    task = _row_to_dict(row)
                    if self._deps_satisfied(cur, task):
                        return task
                return None

    def _deps_satisfied(self, cur: Any, task: dict[str, Any]) -> bool:  # noqa: ANN401  # psycopg2 cursor
        deps: list[str] = task.get("blocked_by") or []
        if not deps:
            return True
        for dep_id in deps:
            cur.execute(_QUERIES["get_dep_status"], (dep_id,))
            row = cur.fetchone()
            if row is None or row["status"] != TaskQueueStatus.COMPLETED.value:
                return False
        return True

    # ------------------------------------------------------------------
    # Claim / Release
    # ------------------------------------------------------------------

    def claim(self, task_id: str, worker_id: str) -> bool:
        now = time.time()
        stale_threshold = now - self._claim_timeout
        with postgres_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
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
                ok = cur.rowcount == 1
            conn.commit()
        return ok

    def release(self, task_id: str, worker_id: str) -> bool:
        now = time.time()
        with postgres_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    _QUERIES["release"],
                    (now, task_id, worker_id),
                )
                ok = cur.rowcount == 1
            conn.commit()
        return ok

    # ------------------------------------------------------------------
    # Heartbeat
    # ------------------------------------------------------------------

    def heartbeat(self, task_id: str, worker_id: str) -> bool:
        now = time.time()
        with postgres_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    _QUERIES["heartbeat"],
                    (now, now, now, task_id, worker_id),
                )
                ok = cur.rowcount == 1
            conn.commit()
        return ok

    # ------------------------------------------------------------------
    # Completion / Failure
    # ------------------------------------------------------------------

    def complete(self, task_id: str, *, output: str = "") -> bool:
        now = time.time()
        with postgres_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    _QUERIES["complete"],
                    (TaskQueueStatus.COMPLETED.value, output, now, task_id),
                )
                ok = cur.rowcount == 1
            conn.commit()
        return ok

    def fail(self, task_id: str, error: str, *, retry: bool = True) -> bool:
        now = time.time()
        with postgres_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(_QUERIES["get_retry_state"], (task_id,))
                row = cur.fetchone()
                if row is None:
                    return False
                retry_count = int(row["retry_count"])
                max_retries = int(row["max_retries"])
                can_retry = retry and retry_count < max_retries
                if can_retry:
                    new_count = retry_count + 1
                    backoff = 30.0 * (2 ** (new_count - 1))
                    cur.execute(
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
                    cur.execute(
                        _QUERIES["fail_permanent"],
                        (TaskQueueStatus.FAILED.value, error, now, task_id),
                    )
            conn.commit()
        return True

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
        sql = "SELECT priority, COUNT(*) AS cnt FROM obscura_tasks WHERE status = %s"
        params: list[Any] = [status_value]
        if worker_id:
            sql += " AND claimed_by = %s"
            params.append(worker_id)
        if project_root is not None:
            sql += " AND project_root = %s"
            params.append(project_root)
        sql += " GROUP BY priority"
        with postgres_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
        return {str(r["priority"]): int(r["cnt"]) for r in rows}

    def get(self, task_id: str) -> dict[str, Any] | None:
        with postgres_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(_QUERIES["get"], (task_id,))
                row = cur.fetchone()
        return _row_to_dict(row) if row else None

    def list_claimed(
        self,
        worker_id: str,
        *,
        project_root: str | None = None,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM obscura_tasks WHERE claimed_by = %s"
        params: list[Any] = [worker_id]
        if project_root is not None:
            sql += " AND project_root = %s"
            params.append(project_root)
        sql += " ORDER BY claimed_at ASC"
        with postgres_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
        return [_row_to_dict(r) for r in rows]

    def reclaim_stale(self) -> int:
        now = time.time()
        stale_threshold = now - self._claim_timeout
        with postgres_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    _QUERIES["reclaim_stale"],
                    (now, TaskQueueStatus.PENDING.value, stale_threshold),
                )
                count = cur.rowcount
            conn.commit()
        return count
