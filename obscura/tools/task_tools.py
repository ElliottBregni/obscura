"""
obscura.tools.task_tools — Background task management tools.

Provides six tools for creating, tracking, and managing background tasks:
  - task_create: Create a new task
  - task_get: Get task details by ID
  - task_list: List all tasks
  - task_update: Update task status/metadata
  - task_output: Get output of a background task
  - task_stop: Stop a running task

Tasks are persisted in SQLite at ``~/.obscura/tasks.db``.
Pattern borrowed from claude-code's TaskCreate/Get/List/Update/Output/Stop tools.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, cast

from obscura.core.tools import tool
from obscura.core.types import ToolSpec


def _get_db() -> sqlite3.Connection:
    """Open (or create) the tasks database."""
    db_path = Path.home() / ".obscura" / "tasks.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            task_id TEXT PRIMARY KEY,
            subject TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            owner TEXT NOT NULL DEFAULT '',
            active_form TEXT NOT NULL DEFAULT '',
            metadata TEXT NOT NULL DEFAULT '{}',
            blocks TEXT NOT NULL DEFAULT '[]',
            blocked_by TEXT NOT NULL DEFAULT '[]',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
    """)
    conn.commit()
    return conn


def _json_error(error: str, **extra: object) -> str:
    payload: dict[str, object] = {"ok": False, "error": error}
    payload.update(extra)
    return json.dumps(payload)


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["metadata"] = json.loads(d.get("metadata", "{}"))
    d["blocks"] = json.loads(d.get("blocks", "[]"))
    d["blocked_by"] = json.loads(d.get("blocked_by", "[]"))
    return d


@tool(
    "task_create",
    "Create a new background task for tracking work.",
    {
        "type": "object",
        "properties": {
            "subject": {"type": "string", "description": "Brief task title."},
            "description": {"type": "string", "description": "Detailed description."},
            "active_form": {"type": "string", "description": "Present continuous form (e.g. 'Running tests')."},
            "metadata": {"type": "object", "description": "Arbitrary metadata."},
        },
        "required": ["subject"],
    },
)
async def task_create(
    subject: str,
    description: str = "",
    active_form: str = "",
    metadata: dict[str, Any] | None = None,
) -> str:
    task_id = uuid.uuid4().hex[:12]
    now = time.time()
    db = _get_db()
    try:
        db.execute(
            """INSERT INTO tasks
               (task_id, subject, description, status, active_form, metadata, created_at, updated_at)
               VALUES (?, ?, ?, 'pending', ?, ?, ?, ?)""",
            (task_id, subject, description, active_form, json.dumps(metadata or {}), now, now),
        )
        db.commit()
    finally:
        db.close()
    return json.dumps({"ok": True, "task_id": task_id, "subject": subject})


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


@tool(
    "task_list",
    "List all tracked tasks.",
    {
        "type": "object",
        "properties": {},
    },
)
async def task_list() -> str:
    db = _get_db()
    try:
        rows = db.execute(
            "SELECT * FROM tasks WHERE status != 'deleted' ORDER BY created_at DESC"
        ).fetchall()
    finally:
        db.close()
    tasks = [_row_to_dict(r) for r in rows]
    # Filter out internal tasks.
    tasks = [t for t in tasks if not t.get("metadata", {}).get("_internal")]
    return json.dumps({"ok": True, "tasks": tasks, "count": len(tasks)})


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
            db.execute("UPDATE tasks SET status = ?, updated_at = ? WHERE task_id = ?", (status, now, task_id))
            updated_fields.append("status")

        if subject and subject != current["subject"]:
            db.execute("UPDATE tasks SET subject = ?, updated_at = ? WHERE task_id = ?", (subject, now, task_id))
            updated_fields.append("subject")

        if description:
            db.execute("UPDATE tasks SET description = ?, updated_at = ? WHERE task_id = ?", (description, now, task_id))
            updated_fields.append("description")

        if active_form:
            db.execute("UPDATE tasks SET active_form = ?, updated_at = ? WHERE task_id = ?", (active_form, now, task_id))
            updated_fields.append("active_form")

        if owner:
            db.execute("UPDATE tasks SET owner = ?, updated_at = ? WHERE task_id = ?", (owner, now, task_id))
            updated_fields.append("owner")

        if add_blocks:
            blocks = current["blocks"]
            blocks.extend(b for b in add_blocks if b not in blocks)
            db.execute("UPDATE tasks SET blocks = ?, updated_at = ? WHERE task_id = ?", (json.dumps(blocks), now, task_id))
            updated_fields.append("blocks")

        if add_blocked_by:
            blocked_by = current["blocked_by"]
            blocked_by.extend(b for b in add_blocked_by if b not in blocked_by)
            db.execute("UPDATE tasks SET blocked_by = ?, updated_at = ? WHERE task_id = ?", (json.dumps(blocked_by), now, task_id))
            updated_fields.append("blocked_by")

        if metadata:
            merged = current["metadata"]
            merged.update(metadata)
            db.execute("UPDATE tasks SET metadata = ?, updated_at = ? WHERE task_id = ?", (json.dumps(merged), now, task_id))
            updated_fields.append("metadata")

        db.commit()
    finally:
        db.close()

    return json.dumps({"ok": True, "task_id": task_id, "updated_fields": updated_fields})


@tool(
    "task_output",
    "Get the output of a background task (for tasks started with run_in_background).",
    {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "Background task ID from run_shell."},
        },
        "required": ["task_id"],
    },
)
async def task_output(task_id: str) -> str:
    from obscura.core.background_tasks import get_background_task_manager
    mgr = get_background_task_manager()
    bg_task = mgr.get(task_id)
    if bg_task is None:
        return _json_error("task_not_found", task_id=task_id)
    return json.dumps({
        "ok": True,
        "task_id": bg_task.task_id,
        "status": bg_task.status,
        "command": bg_task.command,
        "stdout": bg_task.stdout[-50_000:] if bg_task.stdout else "",
        "stderr": bg_task.stderr[-10_000:] if bg_task.stderr else "",
        "exit_code": bg_task.exit_code,
        "started_at": bg_task.started_at,
        "completed_at": bg_task.completed_at,
    })


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
    from obscura.core.background_tasks import get_background_task_manager
    mgr = get_background_task_manager()
    stopped = await mgr.stop(task_id)
    if not stopped:
        return _json_error("task_not_found_or_already_stopped", task_id=task_id)
    return json.dumps({"ok": True, "task_id": task_id, "stopped": True})


def get_task_tool_specs() -> list[ToolSpec]:
    """Return task management tool specs for registration."""
    return [
        cast(ToolSpec, getattr(task_create, "spec")),
        cast(ToolSpec, getattr(task_get, "spec")),
        cast(ToolSpec, getattr(task_list, "spec")),
        cast(ToolSpec, getattr(task_update, "spec")),
        cast(ToolSpec, getattr(task_output, "spec")),
        cast(ToolSpec, getattr(task_stop, "spec")),
    ]
