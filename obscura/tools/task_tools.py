"""obscura.tools.task_tools — Background task management tools.

Provides tools for creating, tracking, and managing tasks:
  - task_create: Create a new task (routed through TaskQueue)
  - task_get: Get task details by ID
  - task_list: List all tasks
  - task_update: Update task status/metadata
  - task_output: Get output of a background shell task
  - task_stop: Stop a running background shell task

Tasks are persisted in SQLite at ``~/.obscura/tasks.db``.

Queue semantics (priority, claiming, heartbeat, retry) are handled by
:mod:`obscura.core.task_queue`.  Use the ``queue_next`` tool for workers
to atomically claim the next ready task.
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import TYPE_CHECKING, Any, cast

from obscura.arbiter.notify import fire_task_complete
from obscura.core.background_tasks import get_background_task_manager
from obscura.core.task_queue import TaskQueue, _open  # noqa: PLC2701  # pyright: ignore[reportPrivateUsage]
from obscura.core.tools import tool
from obscura.kairos.goals import GoalBoard
import logging

logger = logging.getLogger(__name__)


if TYPE_CHECKING:
    from obscura.core.types import ToolSpec


# ---------------------------------------------------------------------------
# DB helpers (kept for task_get / task_list / task_update which read directly)
# ---------------------------------------------------------------------------


def _get_db() -> sqlite3.Connection:
    """Open (or create) the tasks database, applying queue schema."""
    return _open()


def _json_error(error: str, **extra: object) -> str:
    payload: dict[str, object] = {"ok": False, "error": error}
    payload.update(extra)
    return json.dumps(payload)


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


# ---------------------------------------------------------------------------
# task_create
# ---------------------------------------------------------------------------


@tool(
    "task_create",
    "Create a new background task for tracking work.",
    {
        "type": "object",
        "properties": {
            "subject": {"type": "string", "description": "Brief task title."},
            "description": {"type": "string", "description": "Detailed description."},
            "active_form": {
                "type": "string",
                "description": "Present continuous form (e.g. 'Running tests').",
            },
            "priority": {
                "type": "integer",
                "description": (
                    "Priority: 0=critical, 25=high, 50=medium (default), "
                    "75=low, 100=lowest."
                ),
            },
            "goal_id": {
                "type": "string",
                "description": "Parent goal ID to associate this task with.",
            },
            "blocked_by": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Task IDs that must complete before this one is ready.",
            },
            "run_after": {
                "type": "number",
                "description": "Unix timestamp: earliest time this task should be dequeued.",
            },
            "max_retries": {
                "type": "integer",
                "description": "Max automatic retries on failure (default 3).",
            },
            "metadata": {"type": "object", "description": "Arbitrary metadata."},
        },
        "required": ["subject"],
    },
)
async def task_create(
    subject: str,
    description: str = "",
    active_form: str = "",
    priority: int = 50,
    goal_id: str = "",
    blocked_by: list[str] | None = None,
    run_after: float = 0.0,
    max_retries: int = 3,
    metadata: dict[str, Any] | None = None,
) -> str:
    import os

    q = TaskQueue()
    task_id = q.enqueue(
        subject,
        description=description,
        priority=priority,
        goal_id=goal_id,
        blocked_by=blocked_by,
        run_after=run_after,
        max_retries=max_retries,
        metadata=metadata,
        project_root=os.getcwd(),
    )

    # Back-fill active_form if provided (not in TaskQueue.enqueue signature).
    if active_form:
        db = _get_db()
        try:
            db.execute(
                "UPDATE tasks SET active_form = ? WHERE task_id = ?",
                (active_form, task_id),
            )
            db.commit()
        finally:
            db.close()

    return json.dumps({"ok": True, "task_id": task_id, "subject": subject})


# ---------------------------------------------------------------------------
# task_get
# ---------------------------------------------------------------------------


@tool(
    "task_get",
    "Get details of a task by ID.",
    {
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
        },
        "required": ["task_id"],
    },
)
async def task_get(task_id: str) -> str:
    db = _get_db()
    try:
        row = db.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    finally:
        db.close()
    if row is None:
        return json.dumps({"ok": True, "task": None})
    return json.dumps({"ok": True, "task": _row_to_dict(row)})


# ---------------------------------------------------------------------------
# task_list
# ---------------------------------------------------------------------------


@tool(
    "task_list",
    "List all tracked tasks.",
    {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "description": "Filter by status (pending/in_progress/completed/failed). Omit for all.",
            },
            "goal_id": {
                "type": "string",
                "description": "Filter by parent goal ID.",
            },
        },
    },
)
async def task_list(status: str = "", goal_id: str = "") -> str:
    db = _get_db()
    try:
        where_clauses = ["status != 'deleted'"]
        params: list[Any] = []
        if status:
            where_clauses.append("status = ?")
            params.append(status)
        if goal_id:
            where_clauses.append("goal_id = ?")
            params.append(goal_id)
        where = " AND ".join(where_clauses)
        rows = db.execute(
            f"SELECT * FROM tasks WHERE {where} ORDER BY priority ASC, created_at DESC",
            params,
        ).fetchall()
    finally:
        db.close()
    tasks = [_row_to_dict(r) for r in rows]
    # Filter out internal tasks.
    tasks = [t for t in tasks if not t.get("metadata", {}).get("_internal")]
    return json.dumps({"ok": True, "tasks": tasks, "count": len(tasks)})


# ---------------------------------------------------------------------------
# task_update
# ---------------------------------------------------------------------------


@tool(
    "task_update",
    "Update a task's status, subject, description, or metadata.",
    {
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
            "status": {
                "type": "string",
                "enum": ["pending", "in_progress", "completed", "failed", "deleted"],
            },
            "subject": {"type": "string"},
            "description": {"type": "string"},
            "active_form": {"type": "string"},
            "owner": {"type": "string"},
            "priority": {
                "type": "integer",
                "description": "0=critical … 100=lowest.",
            },
            "output": {
                "type": "string",
                "description": "Store task result / output text.",
            },
            "error": {
                "type": "string",
                "description": "Store error message for failed tasks.",
            },
            "add_blocks": {"type": "array", "items": {"type": "string"}},
            "add_blocked_by": {"type": "array", "items": {"type": "string"}},
            "metadata": {"type": "object"},
        },
        "required": ["task_id"],
    },
)
async def task_update(
    task_id: str,
    status: str = "",
    subject: str = "",
    description: str = "",
    active_form: str = "",
    owner: str = "",
    priority: int | None = None,
    output: str = "",
    error: str = "",
    add_blocks: list[str] | None = None,
    add_blocked_by: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> str:
    db = _get_db()
    try:
        row = db.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        if row is None:
            return _json_error("task_not_found", task_id=task_id)

        current = _row_to_dict(row)
        updated_fields: list[str] = []
        now = time.time()

        if status and status != current["status"]:
            if status == "deleted":
                db.execute("DELETE FROM tasks WHERE task_id = ?", (task_id,))
                db.commit()
                return json.dumps({"ok": True, "task_id": task_id, "deleted": True})
            db.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE task_id = ?",
                (status, now, task_id),
            )
            updated_fields.append("status")

        if subject and subject != current["subject"]:
            db.execute(
                "UPDATE tasks SET subject = ?, updated_at = ? WHERE task_id = ?",
                (subject, now, task_id),
            )
            updated_fields.append("subject")

        if description:
            db.execute(
                "UPDATE tasks SET description = ?, updated_at = ? WHERE task_id = ?",
                (description, now, task_id),
            )
            updated_fields.append("description")

        if active_form:
            db.execute(
                "UPDATE tasks SET active_form = ?, updated_at = ? WHERE task_id = ?",
                (active_form, now, task_id),
            )
            updated_fields.append("active_form")

        if owner:
            db.execute(
                "UPDATE tasks SET owner = ?, updated_at = ? WHERE task_id = ?",
                (owner, now, task_id),
            )
            updated_fields.append("owner")

        if priority is not None:
            db.execute(
                "UPDATE tasks SET priority = ?, updated_at = ? WHERE task_id = ?",
                (priority, now, task_id),
            )
            updated_fields.append("priority")

        if output:
            db.execute(
                "UPDATE tasks SET output = ?, updated_at = ? WHERE task_id = ?",
                (output, now, task_id),
            )
            updated_fields.append("output")

        if error:
            db.execute(
                "UPDATE tasks SET error = ?, updated_at = ? WHERE task_id = ?",
                (error, now, task_id),
            )
            updated_fields.append("error")

        if add_blocks:
            blocks = current["blocks"]
            blocks.extend(b for b in add_blocks if b not in blocks)
            db.execute(
                "UPDATE tasks SET blocks = ?, updated_at = ? WHERE task_id = ?",
                (json.dumps(blocks), now, task_id),
            )
            updated_fields.append("blocks")

        if add_blocked_by:
            blocked_by = current["blocked_by"]
            blocked_by.extend(b for b in add_blocked_by if b not in blocked_by)
            db.execute(
                "UPDATE tasks SET blocked_by = ?, updated_at = ? WHERE task_id = ?",
                (json.dumps(blocked_by), now, task_id),
            )
            updated_fields.append("blocked_by")

        if metadata:
            merged = current["metadata"]
            merged.update(metadata)
            db.execute(
                "UPDATE tasks SET metadata = ?, updated_at = ? WHERE task_id = ?",
                (json.dumps(merged), now, task_id),
            )
            updated_fields.append("metadata")

        db.commit()
    finally:
        db.close()

    # Auto-sync goal progress when a task is marked completed via task_update.
    if status == "completed" and "status" in updated_fields:
        _sync_goal_progress(task_id)

    return json.dumps(
        {"ok": True, "task_id": task_id, "updated_fields": updated_fields},
    )


# ---------------------------------------------------------------------------
# task_output  (background shell tasks)
# ---------------------------------------------------------------------------


@tool(
    "task_output",
    "Get the output of a background task (for tasks started with run_in_background).",
    {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "Background task ID from run_shell.",
            },
        },
        "required": ["task_id"],
    },
)
async def task_output(task_id: str) -> str:
    mgr = get_background_task_manager()
    bg_task = mgr.get(task_id)
    if bg_task is None:
        return _json_error("task_not_found", task_id=task_id)
    stdout_full = bg_task.stdout or ""
    stderr_full = bg_task.stderr or ""
    stdout_truncated = len(stdout_full) > 50_000
    stderr_truncated = len(stderr_full) > 10_000
    return json.dumps(
        {
            "ok": True,
            "task_id": bg_task.task_id,
            "status": bg_task.status,
            "command": bg_task.command,
            "stdout": stdout_full[-50_000:],
            "stderr": stderr_full[-10_000:],
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
            "stdout_full_size": len(stdout_full),
            "stderr_full_size": len(stderr_full),
            "exit_code": bg_task.exit_code,
            "started_at": bg_task.started_at,
            "completed_at": bg_task.completed_at,
        },
    )


# ---------------------------------------------------------------------------
# task_stop  (background shell tasks)
# ---------------------------------------------------------------------------


@tool(
    "task_stop",
    "Stop a running background task.",
    {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "Background task ID to stop."},
        },
        "required": ["task_id"],
    },
)
async def task_stop(task_id: str) -> str:
    mgr = get_background_task_manager()
    stopped = await mgr.stop(task_id)
    if not stopped:
        return _json_error("task_not_found_or_already_stopped", task_id=task_id)
    return json.dumps({"ok": True, "task_id": task_id, "stopped": True})


# ---------------------------------------------------------------------------
# queue_next — claim next ready task
# ---------------------------------------------------------------------------


@tool(
    "queue_next",
    "Claim the next ready task from the queue. Returns the task or null if empty.",
    {
        "type": "object",
        "properties": {
            "worker_id": {
                "type": "string",
                "description": "Worker identity. Defaults to 'agent'.",
            },
        },
    },
)
async def queue_next(worker_id: str = "agent") -> str:
    q = TaskQueue()
    q.reclaim_stale()
    task = q.next_ready(worker_id=worker_id)
    if task is None:
        return json.dumps(
            {"ok": True, "task": None, "message": "Queue empty or all blocked."}
        )
    if not q.claim(task["task_id"], worker_id):
        return json.dumps(
            {"ok": True, "task": None, "message": "Claim race lost, retry."}
        )
    return json.dumps({"ok": True, "task": task, "claimed_by": worker_id})


# ---------------------------------------------------------------------------
# queue_complete — mark a claimed task done
# ---------------------------------------------------------------------------


@tool(
    "queue_complete",
    "Mark a claimed task as completed with optional output.",
    {
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
            "output": {"type": "string", "description": "Result summary."},
        },
        "required": ["task_id"],
    },
)
async def queue_complete(task_id: str, output: str = "") -> str:
    q = TaskQueue()
    ok = q.complete(task_id, output=output)
    if not ok:
        return _json_error("task_not_found", task_id=task_id)

    # Sync progress on parent goal if linked.
    _sync_goal_progress(task_id)

    # Fire Arbiter POST_TASK_COMPLETE hook (scoring + audit).
    arbiter_feedback = ""
    task = q.get(task_id)
    if task:
        try:
            result = await fire_task_complete(task)
            if result and result.get("arbiter_feedback"):
                arbiter_feedback = str(result["arbiter_feedback"])
        except ImportError:
            logger.debug("suppressed exception in queue_complete", exc_info=True)

    payload: dict[str, Any] = {"ok": True, "task_id": task_id, "status": "completed"}
    if arbiter_feedback:
        payload["arbiter_feedback"] = arbiter_feedback
    return json.dumps(payload)


# ---------------------------------------------------------------------------
# queue_fail — mark a claimed task failed (with optional retry)
# ---------------------------------------------------------------------------


@tool(
    "queue_fail",
    "Mark a claimed task as failed. Retries automatically if retries remain.",
    {
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
            "error": {"type": "string", "description": "Error description."},
            "retry": {
                "type": "boolean",
                "description": "Allow automatic retry (default true).",
            },
        },
        "required": ["task_id", "error"],
    },
)
async def queue_fail(task_id: str, error: str, retry: bool = True) -> str:
    ok = TaskQueue().fail(task_id, error, retry=retry)
    if not ok:
        return _json_error("task_not_found", task_id=task_id)
    return json.dumps(
        {"ok": True, "task_id": task_id, "error": error, "will_retry": retry}
    )


# ---------------------------------------------------------------------------
# queue_heartbeat — keep a claim alive during long tasks
# ---------------------------------------------------------------------------


@tool(
    "queue_heartbeat",
    "Send a heartbeat to keep a task claim alive during long-running work.",
    {
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
            "worker_id": {"type": "string"},
        },
        "required": ["task_id"],
    },
)
async def queue_heartbeat(task_id: str, worker_id: str = "agent") -> str:
    ok = TaskQueue().heartbeat(task_id, worker_id)
    if not ok:
        return _json_error("heartbeat_failed", task_id=task_id)
    return json.dumps({"ok": True, "task_id": task_id})


# ---------------------------------------------------------------------------
# queue_depth — diagnostics
# ---------------------------------------------------------------------------


@tool(
    "queue_depth",
    "Show queue depth grouped by priority bucket.",
    {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "description": "Filter by status (default 'pending').",
            },
        },
    },
)
async def queue_depth(status: str = "pending") -> str:
    depth = TaskQueue().queue_depth(status=status)
    total = sum(depth.values())
    return json.dumps({"ok": True, "depth": depth, "total": total})


# ---------------------------------------------------------------------------
# Helper: sync goal progress after task completion
# ---------------------------------------------------------------------------


def _sync_goal_progress(task_id: str) -> None:
    """If the completed task is linked to a goal, update goal progress and last_worked."""
    try:
        task = TaskQueue().get(task_id)
        goal_id = task.get("goal_id") if task else None
        if goal_id:
            from datetime import UTC, datetime

            board = GoalBoard()
            board.sync_task_progress(goal_id)
            board.update(goal_id, last_worked=datetime.now(UTC).isoformat())
    except Exception:
        logger.debug("suppressed exception in _sync_goal_progress", exc_info=True)


# ---------------------------------------------------------------------------
# Spec registration
# ---------------------------------------------------------------------------


def get_task_tool_specs() -> list[ToolSpec]:
    """Return task management tool specs for registration."""
    return [
        cast("ToolSpec", cast("Any", task_create).spec),
        cast("ToolSpec", cast("Any", task_get).spec),
        cast("ToolSpec", cast("Any", task_list).spec),
        cast("ToolSpec", cast("Any", task_update).spec),
        cast("ToolSpec", cast("Any", task_output).spec),
        cast("ToolSpec", cast("Any", task_stop).spec),
        cast("ToolSpec", cast("Any", queue_next).spec),
        cast("ToolSpec", cast("Any", queue_complete).spec),
        cast("ToolSpec", cast("Any", queue_fail).spec),
        cast("ToolSpec", cast("Any", queue_heartbeat).spec),
        cast("ToolSpec", cast("Any", queue_depth).spec),
    ]
