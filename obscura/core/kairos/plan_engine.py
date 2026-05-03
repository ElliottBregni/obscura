"""obscura.core.kairos.plan_engine — LLM-powered goal decomposition into Plans.

Calls the configured model to break a Goal into an ordered list of Tasks.
Returns a Plan + list[Task] ready to be persisted.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from obscura.core.kairos.errors import EmptyPlanError, PlanningError
from obscura.core.kairos.types import (
    Goal,
    KairosConfig,
    Plan,
    PlanStatus,
    Task,
    TaskStatus,
)

if TYPE_CHECKING:
    from obscura.core.types import BackendProtocol

logger = logging.getLogger(__name__)

_PLANNING_SYSTEM_PROMPT = """\
You are a task planner. Given a goal, decompose it into a sequence of
discrete, executable tasks. Each task must be atomic — a single focused
action that can be completed in one agent turn.

Return ONLY valid JSON matching this schema:
{
  "rationale": "<why this plan>",
  "tasks": [
    {
      "title": "<short imperative title>",
      "description": "<what to do and why, ≤200 chars>",
      "tool_hint": "<tool name hint or empty string>",
      "depends_on_indices": []   // 0-based indices of tasks this one depends on
    }
  ]
}

Rules:
- Maximum 20 tasks.
- Each task must be independently verifiable.
- Prefer parallelism: only add depends_on when truly sequential.
- tool_hint is a hint (e.g. "bash", "file_read", "web_search"), not a requirement.
- Return only JSON — no markdown, no prose.
"""


class PlanEngine:
    """Decomposes Goals into Plans using an LLM backend.

    Usage::

        engine = PlanEngine(backend, config)
        plan, tasks = await engine.create_plan(goal)
    """

    def __init__(self, backend: BackendProtocol, config: KairosConfig) -> None:
        self._backend = backend
        self._config = config

    async def create_plan(self, goal: Goal) -> tuple[Plan, list[Task]]:
        """Call the LLM to decompose *goal* into an executable Plan.

        Returns:
            (Plan, list[Task]) — ready to persist; Plan is in DRAFT status.

        Raises:
            PlanningError: LLM failed or returned invalid JSON.
            EmptyPlanError: LLM returned zero tasks.
        """
        prompt = self._build_planning_prompt(goal)
        raw = await self._call_model(prompt)
        data = self._parse_response(raw, goal.goal_id)

        plan_id = str(uuid.uuid4())
        now = datetime.now(UTC)

        task_id_map: dict[int, str] = {}
        tasks: list[Task] = []

        for i, t in enumerate(data["tasks"]):
            tid = str(uuid.uuid4())
            task_id_map[i] = tid
            depends_on = tuple(
                task_id_map[j]
                for j in t.get("depends_on_indices", [])
                if j < i and j in task_id_map
            )
            tasks.append(
                Task(
                    task_id=tid,
                    goal_id=goal.goal_id,
                    plan_id=plan_id,
                    title=t["title"],
                    description=t.get("description", ""),
                    order_index=i,
                    depends_on=depends_on,
                    tool_hint=t.get("tool_hint", ""),
                    max_retries=self._config.default_budget.max_retries_per_task,
                    status=TaskStatus.PENDING,
                    created_at=now,
                )
            )

        plan = Plan(
            plan_id=plan_id,
            goal_id=goal.goal_id,
            revision=0,
            rationale=data.get("rationale", ""),
            task_ids=tuple(t.task_id for t in tasks),
            status=PlanStatus.DRAFT,
            created_at=now,
        )
        return plan, tasks

    async def revise_plan(
        self,
        goal: Goal,
        current_plan: Plan,
        completed_task_ids: list[str],
        failure_context: str,
        revision: int,
    ) -> tuple[Plan, list[Task]]:
        """Create a revised plan after partial failure.

        Args:
            goal: The original goal.
            current_plan: The plan being superseded.
            completed_task_ids: Tasks already successfully completed.
            failure_context: What went wrong (error message + context).
            revision: New revision number.
        """
        prompt = self._build_revision_prompt(
            goal, current_plan, completed_task_ids, failure_context
        )
        raw = await self._call_model(prompt)
        data = self._parse_response(raw, goal.goal_id)

        plan_id = str(uuid.uuid4())
        now = datetime.now(UTC)

        task_id_map: dict[int, str] = {}
        tasks: list[Task] = []

        for i, t in enumerate(data["tasks"]):
            tid = str(uuid.uuid4())
            task_id_map[i] = tid
            depends_on = tuple(
                task_id_map[j]
                for j in t.get("depends_on_indices", [])
                if j < i and j in task_id_map
            )
            tasks.append(
                Task(
                    task_id=tid,
                    goal_id=goal.goal_id,
                    plan_id=plan_id,
                    title=t["title"],
                    description=t.get("description", ""),
                    order_index=i,
                    depends_on=depends_on,
                    tool_hint=t.get("tool_hint", ""),
                    max_retries=self._config.default_budget.max_retries_per_task,
                    status=TaskStatus.PENDING,
                    created_at=now,
                )
            )

        plan = Plan(
            plan_id=plan_id,
            goal_id=goal.goal_id,
            revision=revision,
            rationale=data.get("rationale", "Revised after failure"),
            task_ids=tuple(t.task_id for t in tasks),
            status=PlanStatus.DRAFT,
            created_at=now,
        )
        return plan, tasks

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _build_planning_prompt(self, goal: Goal) -> str:
        criteria = "\n".join(f"  - {c}" for c in goal.success_criteria)
        return (
            f"Goal: {goal.title}\n\n"
            f"Description: {goal.description}\n\n"
            f"Success criteria:\n{criteria or '  (none specified)'}\n\n"
            "Decompose this goal into an executable task list."
        )

    def _build_revision_prompt(
        self,
        goal: Goal,
        current_plan: Plan,
        completed_task_ids: list[str],
        failure_context: str,
    ) -> str:
        completed_count = len(completed_task_ids)
        total = len(current_plan.task_ids)
        return (
            f"Goal: {goal.title}\n\n"
            f"Description: {goal.description}\n\n"
            f"Progress: {completed_count}/{total} tasks completed.\n\n"
            f"Failure context:\n{failure_context}\n\n"
            "Create a REVISED task list for the remaining work. "
            "Skip already-completed tasks. Focus on what still needs to be done."
        )

    async def _call_model(self, prompt: str) -> str:
        """Stream the model response and collect into a single string."""
        chunks: list[str] = []
        try:
            stream_iter: Any = self._backend.stream(  # type: ignore[attr-defined]
                messages=[{"role": "user", "content": prompt}],
                system=_PLANNING_SYSTEM_PROMPT,
                max_tokens=2048,
            )
            async for chunk in stream_iter:  # pyright: ignore[reportUnknownVariableType]
                text_attr: Any = getattr(
                    chunk,  # pyright: ignore[reportUnknownArgumentType]
                    "text",
                    None,
                )
                if isinstance(text_attr, str):
                    chunks.append(text_attr)
                elif isinstance(chunk, str):
                    chunks.append(chunk)
        except Exception as exc:
            raise PlanningError(f"LLM call failed during planning: {exc}") from exc
        return "".join(chunks)

    def _parse_response(self, raw: str, goal_id: str) -> dict[str, Any]:
        """Parse the LLM JSON response."""
        # Strip markdown fences if present
        text = raw.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            text = "\n".join(lines[1:-1]) if len(lines) > 2 else text

        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise PlanningError(
                f"Invalid JSON from planner: {exc}",
                goal_id=goal_id,
            ) from exc

        tasks = data.get("tasks", [])
        if not tasks:
            raise EmptyPlanError("Planner returned 0 tasks", goal_id=goal_id)
        if len(tasks) > self._config.max_plan_tasks:
            tasks = tasks[: self._config.max_plan_tasks]
            data["tasks"] = tasks
            logger.warning(
                "Plan truncated to %d tasks (goal %s)",
                self._config.max_plan_tasks,
                goal_id,
            )
        return data
