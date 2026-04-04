"""obscura.core.kairos.goal_store — SQLite-backed persistence for Goals, Plans, Tasks.

All writes are synchronous SQLite. The runtime layer is async but calls
these helpers from a thread executor to avoid blocking the event loop.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from obscura.core.kairos.errors import GoalNotFoundError, TaskNotFoundError
from obscura.core.kairos.schema import init_kairos_schema
from obscura.core.kairos.types import (
    BudgetUsage,
    Checkpoint,
    CheckpointKind,
    Goal,
    GoalBudget,
    GoalStatus,
    Intervention,
    InterventionKind,
    KairosEvent,
    Plan,
    PlanStatus,
    Task,
    TaskResult,
    TaskStatus,
)

logger = logging.getLogger(__name__)

_ISO = "%Y-%m-%dT%H:%M:%S.%f+00:00"


def _now_str() -> str:
    return datetime.now(UTC).strftime(_ISO)


def _dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


class GoalStore:
    """SQLite-backed store for all Kairos domain objects.

    Thread-safe: uses a single connection with WAL mode.
    Call :meth:`close` when done.
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        init_kairos_schema(self._conn)

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------------
    # Goals
    # ------------------------------------------------------------------

    def create_goal(self, goal: Goal) -> None:
        self._conn.execute(
            """
            INSERT INTO kairos_goals (
                goal_id, title, description, success_criteria,
                session_id, owner_id, status,
                budget_json, tool_allowlist, tool_blocklist,
                tags, metadata, created_at, deadline
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                goal.goal_id,
                goal.title,
                goal.description,
                json.dumps(list(goal.success_criteria)),
                goal.session_id,
                goal.owner_id,
                goal.status.value,
                json.dumps(
                    {
                        "max_tasks": goal.budget.max_tasks,
                        "max_turns": goal.budget.max_turns,
                        "max_wall_seconds": goal.budget.max_wall_seconds,
                        "max_tokens": goal.budget.max_tokens,
                        "max_retries_per_task": goal.budget.max_retries_per_task,
                    }
                ),
                json.dumps(list(goal.tool_allowlist)),
                json.dumps(list(goal.tool_blocklist)),
                json.dumps(list(goal.tags)),
                json.dumps(goal.metadata),
                goal.created_at.strftime(_ISO),
                goal.deadline.strftime(_ISO) if goal.deadline else None,
            ),
        )
        self._conn.commit()
        # Initialize budget tracking row
        self._conn.execute(
            "INSERT OR IGNORE INTO kairos_budget_usage (goal_id, updated_at) VALUES (?, ?)",
            (goal.goal_id, _now_str()),
        )
        self._conn.commit()

    def get_goal(self, goal_id: str) -> Goal:
        row = self._conn.execute(
            "SELECT * FROM kairos_goals WHERE goal_id = ?", (goal_id,)
        ).fetchone()
        if not row:
            raise GoalNotFoundError(f"Goal not found: {goal_id}", goal_id=goal_id)
        return self._row_to_goal(row)

    def update_goal_status(
        self,
        goal_id: str,
        status: GoalStatus,
        *,
        started_at: datetime | None = None,
        completed_at: datetime | None = None,
    ) -> None:
        self._conn.execute(
            """
            UPDATE kairos_goals
               SET status = ?,
                   started_at = COALESCE(?, started_at),
                   completed_at = COALESCE(?, completed_at)
             WHERE goal_id = ?
            """,
            (
                status.value,
                started_at.strftime(_ISO) if started_at else None,
                completed_at.strftime(_ISO) if completed_at else None,
                goal_id,
            ),
        )
        self._conn.commit()

    def list_goals(
        self,
        status: GoalStatus | None = None,
        owner_id: str = "",
        limit: int = 100,
    ) -> list[Goal]:
        if status:
            rows = self._conn.execute(
                "SELECT * FROM kairos_goals WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                (status.value, limit),
            ).fetchall()
        elif owner_id:
            rows = self._conn.execute(
                "SELECT * FROM kairos_goals WHERE owner_id = ? ORDER BY created_at DESC LIMIT ?",
                (owner_id, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM kairos_goals ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row_to_goal(r) for r in rows]

    def _row_to_goal(self, row: tuple[Any, ...]) -> Goal:
        (
            goal_id,
            title,
            description,
            success_criteria_json,
            session_id,
            owner_id,
            status_str,
            budget_json,
            tool_allowlist_json,
            tool_blocklist_json,
            tags_json,
            metadata_json,
            created_at_str,
            started_at_str,
            completed_at_str,
            deadline_str,
        ) = row
        b = json.loads(budget_json)
        return Goal(
            goal_id=goal_id,
            title=title,
            description=description,
            success_criteria=tuple(json.loads(success_criteria_json)),
            session_id=session_id,
            owner_id=owner_id,
            status=GoalStatus(status_str),
            budget=GoalBudget(**b),
            tool_allowlist=tuple(json.loads(tool_allowlist_json)),
            tool_blocklist=tuple(json.loads(tool_blocklist_json)),
            tags=tuple(json.loads(tags_json)),
            metadata=json.loads(metadata_json),
            created_at=datetime.fromisoformat(created_at_str),
            started_at=_dt(started_at_str),
            completed_at=_dt(completed_at_str),
            deadline=_dt(deadline_str),
        )

    # ------------------------------------------------------------------
    # Plans
    # ------------------------------------------------------------------

    def create_plan(self, plan: Plan) -> None:
        self._conn.execute(
            """
            INSERT INTO kairos_plans (
                plan_id, goal_id, revision, rationale,
                task_ids, status, metadata, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                plan.plan_id,
                plan.goal_id,
                plan.revision,
                plan.rationale,
                json.dumps(list(plan.task_ids)),
                plan.status.value,
                json.dumps(plan.metadata),
                plan.created_at.strftime(_ISO),
            ),
        )
        self._conn.commit()

    def get_active_plan(self, goal_id: str) -> Plan | None:
        row = self._conn.execute(
            """
            SELECT * FROM kairos_plans
             WHERE goal_id = ? AND status = 'active'
             ORDER BY revision DESC LIMIT 1
            """,
            (goal_id,),
        ).fetchone()
        return self._row_to_plan(row) if row else None

    def update_plan_status(self, plan_id: str, status: PlanStatus) -> None:
        completed_at = (
            _now_str() if status in (PlanStatus.COMPLETED, PlanStatus.FAILED) else None
        )
        self._conn.execute(
            "UPDATE kairos_plans SET status = ?, completed_at = COALESCE(?, completed_at) WHERE plan_id = ?",
            (status.value, completed_at, plan_id),
        )
        self._conn.commit()

    def _row_to_plan(self, row: tuple[Any, ...]) -> Plan:
        (
            plan_id,
            goal_id,
            revision,
            rationale,
            task_ids_json,
            status_str,
            metadata_json,
            created_at_str,
            completed_at_str,
        ) = row
        return Plan(
            plan_id=plan_id,
            goal_id=goal_id,
            revision=revision,
            rationale=rationale,
            task_ids=tuple(json.loads(task_ids_json)),
            status=PlanStatus(status_str),
            created_at=datetime.fromisoformat(created_at_str),
            completed_at=_dt(completed_at_str),
            metadata=json.loads(metadata_json),
        )

    # ------------------------------------------------------------------
    # Tasks
    # ------------------------------------------------------------------

    def create_task(self, task: Task) -> None:
        self._conn.execute(
            """
            INSERT INTO kairos_tasks (
                task_id, goal_id, plan_id, title, description,
                order_index, depends_on, tool_hint, model,
                max_retries, retry_count, status, metadata, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task.task_id,
                task.goal_id,
                task.plan_id,
                task.title,
                task.description,
                task.order_index,
                json.dumps(list(task.depends_on)),
                task.tool_hint,
                task.model,
                task.max_retries,
                task.retry_count,
                task.status.value,
                json.dumps(task.metadata),
                task.created_at.strftime(_ISO),
            ),
        )
        self._conn.commit()

    def get_task(self, task_id: str) -> Task:
        row = self._conn.execute(
            "SELECT * FROM kairos_tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        if not row:
            raise TaskNotFoundError(f"Task not found: {task_id}", task_id=task_id)
        return self._row_to_task(row)

    def list_tasks(self, plan_id: str) -> list[Task]:
        rows = self._conn.execute(
            "SELECT * FROM kairos_tasks WHERE plan_id = ? ORDER BY order_index",
            (plan_id,),
        ).fetchall()
        return [self._row_to_task(r) for r in rows]

    def update_task_status(
        self,
        task_id: str,
        status: TaskStatus,
        *,
        retry_count: int | None = None,
        started_at: datetime | None = None,
        completed_at: datetime | None = None,
    ) -> None:
        self._conn.execute(
            """
            UPDATE kairos_tasks
               SET status = ?,
                   retry_count = COALESCE(?, retry_count),
                   started_at = COALESCE(?, started_at),
                   completed_at = COALESCE(?, completed_at)
             WHERE task_id = ?
            """,
            (
                status.value,
                retry_count,
                started_at.strftime(_ISO) if started_at else None,
                completed_at.strftime(_ISO) if completed_at else None,
                task_id,
            ),
        )
        self._conn.commit()

    def save_task_result(self, result: TaskResult) -> None:
        result_id = str(uuid.uuid4())
        self._conn.execute(
            """
            INSERT INTO kairos_task_results (
                result_id, task_id, goal_id, plan_id, status,
                summary, output, error,
                turns_used, tokens_used, elapsed_ms, completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                result_id,
                result.task_id,
                result.goal_id,
                result.plan_id,
                result.status.value,
                result.summary,
                result.output,
                result.error,
                result.turns_used,
                result.tokens_used,
                result.elapsed_ms,
                result.completed_at.strftime(_ISO),
            ),
        )
        self._conn.commit()

    def _row_to_task(self, row: tuple[Any, ...]) -> Task:
        (
            task_id,
            goal_id,
            plan_id,
            title,
            description,
            order_index,
            depends_on_json,
            tool_hint,
            model,
            max_retries,
            retry_count,
            status_str,
            metadata_json,
            created_at_str,
            started_at_str,
            completed_at_str,
        ) = row
        return Task(
            task_id=task_id,
            goal_id=goal_id,
            plan_id=plan_id,
            title=title,
            description=description,
            order_index=order_index,
            depends_on=tuple(json.loads(depends_on_json)),
            tool_hint=tool_hint,
            model=model,
            max_retries=max_retries,
            retry_count=retry_count,
            status=TaskStatus(status_str),
            metadata=json.loads(metadata_json),
            created_at=datetime.fromisoformat(created_at_str),
            started_at=_dt(started_at_str),
            completed_at=_dt(completed_at_str),
        )

    # ------------------------------------------------------------------
    # Checkpoints
    # ------------------------------------------------------------------

    def create_checkpoint(self, cp: Checkpoint) -> None:
        self._conn.execute(
            """
            INSERT INTO kairos_checkpoints (
                checkpoint_id, goal_id, plan_id, kind,
                completed_task_ids, pending_task_ids,
                summary, learnings, next_steps,
                budget_usage_json, metadata, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cp.checkpoint_id,
                cp.goal_id,
                cp.plan_id,
                cp.kind.value,
                json.dumps(list(cp.completed_task_ids)),
                json.dumps(list(cp.pending_task_ids)),
                cp.summary,
                cp.learnings,
                cp.next_steps,
                json.dumps(
                    {
                        "tasks_run": cp.budget_usage.tasks_run,
                        "turns_used": cp.budget_usage.turns_used,
                        "elapsed_seconds": cp.budget_usage.elapsed_seconds,
                        "tokens_used": cp.budget_usage.tokens_used,
                        "retries_used": cp.budget_usage.retries_used,
                    }
                ),
                json.dumps(cp.metadata),
                cp.created_at.strftime(_ISO),
            ),
        )
        self._conn.commit()

    def get_latest_checkpoint(self, goal_id: str) -> Checkpoint | None:
        row = self._conn.execute(
            """
            SELECT * FROM kairos_checkpoints
             WHERE goal_id = ?
             ORDER BY created_at DESC LIMIT 1
            """,
            (goal_id,),
        ).fetchone()
        if not row:
            return None
        (
            cp_id,
            goal_id,
            plan_id,
            kind_str,
            completed_json,
            pending_json,
            summary,
            learnings,
            next_steps,
            budget_json,
            metadata_json,
            created_at_str,
        ) = row
        b = json.loads(budget_json)
        return Checkpoint(
            checkpoint_id=cp_id,
            goal_id=goal_id,
            plan_id=plan_id,
            kind=CheckpointKind(kind_str),
            completed_task_ids=tuple(json.loads(completed_json)),
            pending_task_ids=tuple(json.loads(pending_json)),
            summary=summary,
            learnings=learnings,
            next_steps=next_steps,
            budget_usage=BudgetUsage(**b),
            metadata=json.loads(metadata_json),
            created_at=datetime.fromisoformat(created_at_str),
        )

    # ------------------------------------------------------------------
    # Interventions
    # ------------------------------------------------------------------

    def create_intervention(self, iv: Intervention) -> None:
        self._conn.execute(
            """
            INSERT INTO kairos_interventions (
                intervention_id, goal_id, task_id, kind,
                question, context, options,
                response, resolved, metadata, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                iv.intervention_id,
                iv.goal_id,
                iv.task_id,
                iv.kind.value,
                iv.question,
                iv.context,
                json.dumps(list(iv.options)),
                iv.response,
                1 if iv.resolved else 0,
                json.dumps(iv.metadata),
                iv.created_at.strftime(_ISO),
            ),
        )
        self._conn.commit()

    def resolve_intervention(self, intervention_id: str, response: str) -> None:
        self._conn.execute(
            """
            UPDATE kairos_interventions
               SET response = ?, resolved = 1, resolved_at = ?
             WHERE intervention_id = ?
            """,
            (response, _now_str(), intervention_id),
        )
        self._conn.commit()

    def list_pending_interventions(self, goal_id: str) -> list[Intervention]:
        rows = self._conn.execute(
            "SELECT * FROM kairos_interventions WHERE goal_id = ? AND resolved = 0 ORDER BY created_at",
            (goal_id,),
        ).fetchall()
        return [self._row_to_intervention(r) for r in rows]

    def _row_to_intervention(self, row: tuple[Any, ...]) -> Intervention:
        (
            iv_id,
            goal_id,
            task_id,
            kind_str,
            question,
            context,
            options_json,
            response,
            resolved,
            metadata_json,
            created_at_str,
            resolved_at_str,
        ) = row
        return Intervention(
            intervention_id=iv_id,
            goal_id=goal_id,
            task_id=task_id,
            kind=InterventionKind(kind_str),
            question=question,
            context=context,
            options=tuple(json.loads(options_json)),
            response=response,
            resolved=bool(resolved),
            metadata=json.loads(metadata_json),
            created_at=datetime.fromisoformat(created_at_str),
            resolved_at=_dt(resolved_at_str),
        )

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    def append_event(self, event: KairosEvent) -> None:
        self._conn.execute(
            """
            INSERT INTO kairos_events (goal_id, plan_id, task_id, kind, payload, timestamp)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                event.goal_id,
                event.plan_id,
                event.task_id,
                event.kind.value,
                json.dumps(event.payload),
                event.timestamp.strftime(_ISO),
            ),
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # Budget usage
    # ------------------------------------------------------------------

    def get_budget_usage(self, goal_id: str) -> BudgetUsage:
        row = self._conn.execute(
            "SELECT tasks_run, turns_used, elapsed_seconds, tokens_used, retries_used FROM kairos_budget_usage WHERE goal_id = ?",
            (goal_id,),
        ).fetchone()
        if not row:
            return BudgetUsage()
        return BudgetUsage(
            tasks_run=row[0],
            turns_used=row[1],
            elapsed_seconds=row[2],
            tokens_used=row[3],
            retries_used=row[4],
        )

    def update_budget_usage(self, goal_id: str, usage: BudgetUsage) -> None:
        self._conn.execute(
            """
            INSERT INTO kairos_budget_usage (goal_id, tasks_run, turns_used, elapsed_seconds, tokens_used, retries_used, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(goal_id) DO UPDATE SET
                tasks_run = excluded.tasks_run,
                turns_used = excluded.turns_used,
                elapsed_seconds = excluded.elapsed_seconds,
                tokens_used = excluded.tokens_used,
                retries_used = excluded.retries_used,
                updated_at = excluded.updated_at
            """,
            (
                goal_id,
                usage.tasks_run,
                usage.turns_used,
                usage.elapsed_seconds,
                usage.tokens_used,
                usage.retries_used,
                _now_str(),
            ),
        )
        self._conn.commit()
