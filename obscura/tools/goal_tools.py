"""obscura.tools.goal_tools — Goal board management tool.

Single ``goal`` tool with ``action`` discriminator for all goal operations:
create, list, get, update, complete, abandon, add_task.

Goals are persisted as markdown files at ``~/.obscura/goals/``.
Goal lifecycle events are also emitted to the vector memory store
for semantic retrieval.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import timedelta
from typing import TYPE_CHECKING, Any, cast

from obscura.arbiter.notify import fire_goal_transition
from obscura.auth.cli_user import current_cli_user
from obscura.core.tools import tool
from obscura.tools.task_tools import _get_db  # pyright: ignore[reportPrivateUsage]
from obscura.vector_memory.vector_memory import VectorMemoryStore

if TYPE_CHECKING:
    from obscura.core.types import ToolSpec

_logger = logging.getLogger(__name__)


def _board() -> Any:
    # lazy: avoid circular dep with obscura.kairos (kairos.dream imports get_goal_tool_specs from here)
    from obscura.kairos.goals import GoalBoard

    return GoalBoard()


def _notify_vault(goal_id: str) -> None:
    """Best-effort vault sync on goal mutation."""
    try:
        # lazy: avoid circular dep with obscura.kairos (kairos.dream imports get_goal_tool_specs from here)
        from obscura.kairos.vault_sync import notify_goal_changed

        notify_goal_changed(goal_id)
    except Exception:
        _logger.debug("suppressed exception in _notify_vault", exc_info=True)


def _notify_arbiter(goal: Any) -> None:
    """Best-effort Arbiter notification on goal transition."""
    try:
        import asyncio

        goal_dict = _goal_dict(goal)
        # Resolve linked task statuses for the Arbiter.
        task_statuses: list[str] = []
        if goal.tasks:
            try:
                db = _get_db()
                for tid in goal.tasks:
                    row = db.execute(
                        "SELECT status FROM tasks WHERE task_id = ?", (tid,)
                    ).fetchone()
                    task_statuses.append(row["status"] if row else "unknown")
                db.close()
            except Exception:
                _logger.debug("suppressed exception in _notify_arbiter", exc_info=True)

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(
                fire_goal_transition(goal_dict, linked_task_statuses=task_statuses)
            )
        except RuntimeError:
            # No running loop — skip async fire.
            _logger.debug("suppressed exception in _notify_arbiter", exc_info=True)
    except ImportError:
        _logger.debug("suppressed exception in _notify_arbiter", exc_info=True)
    except Exception:
        _logger.debug("suppressed exception in _notify_arbiter", exc_info=True)


def _emit_goal_event(
    goal_id: str,
    title: str,
    event: str,
    detail: str = "",
    priority: str = "medium",
) -> None:
    """Emit a goal lifecycle event to vector memory for semantic search."""
    try:
        store = VectorMemoryStore.for_user(_get_current_user())
        from datetime import UTC, datetime

        now = datetime.now(UTC).isoformat()
        text = f"Goal '{title}' {event}"
        if detail:
            text += f": {detail}"
        store.set(
            key=f"goal:{goal_id}:event:{now}",
            text=text,
            namespace="goals",
            memory_type="episode",
            metadata={
                "goal_id": goal_id,
                "event_type": event,
                "priority": priority,
            },
            ttl=timedelta(days=90),
        )
    except Exception:
        _logger.debug("Could not emit goal event to vector memory", exc_info=True)


def _get_current_user() -> Any:
    """Best-effort retrieval of the current authenticated user."""
    return current_cli_user()


def _json_ok(**data: object) -> str:
    payload: dict[str, object] = {"ok": True}
    payload.update(data)
    return json.dumps(payload)


def _json_error(error: str, **extra: object) -> str:
    payload: dict[str, object] = {"ok": False, "error": error}
    payload.update(extra)
    return json.dumps(payload)


def _goal_dict(goal: Any) -> dict[str, Any]:
    d = asdict(goal)
    d.pop("path", None)
    for k in ("acceptance_criteria", "depends_on", "tasks"):
        if k in d and isinstance(d[k], tuple):
            d[k] = list(d[k])
    return d


# ---------------------------------------------------------------------------
# Single unified tool
# ---------------------------------------------------------------------------


@tool(
    "goal",
    (
        "Manage the goal board. "
        "create: title + priority/context/acceptance_criteria/depends_on. "
        "list: optional status filter. "
        "get/complete/abandon: goal_id required. "
        "update: goal_id + any of title/priority/progress/acceptance_criteria/last_worked. "
        "add_task: goal_id + task_id."
    ),
    {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "create",
                    "list",
                    "get",
                    "update",
                    "complete",
                    "abandon",
                    "add_task",
                ],
                "description": "The operation to perform.",
            },
            # create params
            "title": {"type": "string", "description": "(create) Goal title."},
            "priority": {
                "type": "string",
                "enum": ["critical", "high", "medium", "low"],
                "description": "(create/update) Priority level.",
            },
            "context": {
                "type": "string",
                "description": "(create) Background context or motivation.",
            },
            "acceptance_criteria": {
                "type": "array",
                "items": {"type": "string"},
                "description": "(create/update) Conditions that define completion.",
            },
            "depends_on": {
                "type": "array",
                "items": {"type": "string"},
                "description": "(create) Goal IDs this goal depends on.",
            },
            # get/update/complete/abandon/add_task params
            "goal_id": {
                "type": "string",
                "description": "(get/update/complete/abandon/add_task) Goal ID.",
            },
            # update params
            "progress": {
                "type": "integer",
                "minimum": 0,
                "maximum": 100,
                "description": "(update) Completion percentage.",
            },
            "last_worked": {
                "type": "string",
                "description": "(update) ISO date of last progress.",
            },
            # list params
            "status": {
                "type": "string",
                "description": "(list) Filter by status. Omit for all.",
            },
            # abandon params
            "reason": {
                "type": "string",
                "description": "(abandon) Why the goal was abandoned.",
            },
            # add_task params
            "task_id": {
                "type": "string",
                "description": "(add_task) Task ID to link.",
            },
        },
        "required": ["action"],
    },
)
def goal_tool(
    action: str,
    # create
    title: str = "",
    priority: str = "medium",
    context: str = "",
    acceptance_criteria: list[str] | None = None,
    depends_on: list[str] | None = None,
    # get/update/complete/abandon/add_task
    goal_id: str = "",
    # update
    progress: int | None = None,
    last_worked: str | None = None,
    # list
    status: str = "",
    # abandon
    reason: str = "",
    # add_task
    task_id: str = "",
) -> str:
    board = _board()

    if action == "create":
        if not title:
            return _json_error(
                "missing_title", detail="'title' is required for create."
            )
        g = board.create(
            title,
            priority=priority,
            context=context,
            acceptance_criteria=acceptance_criteria,
            depends_on=depends_on,
        )
        # project_root is captured inside GoalBoard.create() via os.getcwd()
        _emit_goal_event(g.id, title, "created", f"priority={priority}", priority)
        _notify_vault(g.id)
        return _json_ok(goal_id=g.id, goal=_goal_dict(g))

    if action == "list":
        goals = board.load_all()
        if status:
            goals = [g for g in goals if g.status == status]
        return _json_ok(goals=[_goal_dict(g) for g in goals], count=len(goals))

    if action == "get":
        if not goal_id:
            return _json_error("missing_goal_id")
        g = board.load(goal_id)
        if g is None:
            return _json_error("goal_not_found", goal_id=goal_id)
        return _json_ok(goal=_goal_dict(g))

    if action == "update":
        if not goal_id:
            return _json_error("missing_goal_id")
        fields: dict[str, Any] = {}
        if title:
            fields["title"] = title
        if priority != "medium":
            fields["priority"] = priority
        if acceptance_criteria is not None:
            fields["acceptance_criteria"] = acceptance_criteria
        if progress is not None:
            fields["progress"] = progress
        if last_worked is not None:
            fields["last_worked"] = last_worked
        g = board.update(goal_id, **fields)
        if g is None:
            return _json_error("goal_not_found_or_invalid_transition", goal_id=goal_id)
        detail = ", ".join(f"{k}={v}" for k, v in fields.items() if v is not None)
        _emit_goal_event(goal_id, g.title, "updated", detail, g.priority)
        _notify_vault(goal_id)
        return _json_ok(goal=_goal_dict(g))

    if action == "complete":
        if not goal_id:
            return _json_error("missing_goal_id")
        g = board.complete(goal_id)
        if g is None:
            return _json_error("goal_not_found_or_invalid_transition", goal_id=goal_id)
        _emit_goal_event(goal_id, g.title, "completed", "100%", g.priority)
        _notify_vault(goal_id)
        _notify_arbiter(g)
        return _json_ok(goal=_goal_dict(g))

    if action == "abandon":
        if not goal_id:
            return _json_error("missing_goal_id")
        g = board.abandon(goal_id, reason)
        if g is None:
            return _json_error("goal_not_found_or_invalid_transition", goal_id=goal_id)
        _emit_goal_event(goal_id, g.title, "abandoned", reason, g.priority)
        _notify_vault(goal_id)
        _notify_arbiter(g)
        return _json_ok(goal=_goal_dict(g))

    if action == "add_task":
        if not goal_id or not task_id:
            return _json_error("missing_params", detail="goal_id and task_id required.")
        g = board.link_task(goal_id, task_id)
        if g is None:
            return _json_error("goal_not_found", goal_id=goal_id)
        _notify_vault(goal_id)
        return _json_ok(goal=_goal_dict(g))

    return _json_error("invalid_action", detail=f"Unknown action: {action}")


def get_goal_tool_specs() -> list[ToolSpec]:
    """Return goal management tool specs for registration."""
    return [cast("ToolSpec", getattr(goal_tool, "spec"))]
