"""obscura.core.kairos.kairos — The Kairos autonomous goal runtime.

Manages the full lifecycle of Goal-driven autonomous execution:

    create_goal → plan → execute_tasks → checkpoint → complete
                              ↑                ↓
                           replan ← failure/intervention

Usage::

    from obscura.core.kairos import Kairos, KairosConfig
    from obscura.core.kairos.types import GoalBudget

    kairos = Kairos(db_path="~/.obscura/kairos.db", agent_loop=loop)

    goal_id = await kairos.create_goal(
        title="Refactor the auth module",
        description="...",
        success_criteria=["All tests pass", "No mypy errors"],
        budget=GoalBudget(max_turns=50, max_wall_seconds=600),
    )

    async for event in kairos.run(goal_id):
        print(event.kind, event.payload)
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from obscura.core.kairos.errors import (
    BudgetExceededError,
    GoalStateError,
    InterventionRequiredError,
    KairosRuntimeError,
    PlanningError,
)
from obscura.core.kairos.goal_store import GoalStoreProtocol, create_goal_store
from obscura.core.enums.lifecycle import (
    KAIROS_VALID_GOAL_TRANSITIONS,
    GoalStatus,
    KairosTaskStatus,
    PlanStatus,
)
from obscura.core.kairos.plan_engine import PlanEngine
from obscura.core.kairos.task_runner import TaskRunner
from obscura.core.kairos.types import (
    BudgetUsage,
    Checkpoint,
    CheckpointKind,
    Goal,
    GoalBudget,
    KairosConfig,
    KairosEvent,
    KairosEventKind,
    Plan,
    Task,
)

if TYPE_CHECKING:
    from obscura.core.agent_loop_v2 import AgentLoopV2
    from obscura.core.types import BackendProtocol

logger = logging.getLogger(__name__)


class Kairos:
    """Autonomous goal runtime.

    Orchestrates Goal → Plan → Task → Checkpoint → (repeat or complete).

    Thread-safe for concurrent goals (each goal has its own async lock).
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        agent_loop: AgentLoopV2,
        backend: BackendProtocol | None = None,
        config: KairosConfig | None = None,
    ) -> None:
        self._db_path = Path(db_path)
        self._config = config or KairosConfig()
        self._store: GoalStoreProtocol = create_goal_store(self._db_path)
        self._agent_loop = agent_loop
        self._backend = backend
        self._task_runner = TaskRunner(agent_loop, self._store, self._config)
        self._plan_engine: PlanEngine | None = None
        self._goal_locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def create_goal(
        self,
        title: str,
        description: str,
        *,
        success_criteria: list[str] | None = None,
        session_id: str = "",
        owner_id: str = "",
        budget: GoalBudget | None = None,
        tool_allowlist: list[str] | None = None,
        tool_blocklist: list[str] | None = None,
        tags: list[str] | None = None,
        deadline: datetime | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> str:
        """Create and persist a new Goal. Returns goal_id."""
        goal_id = str(uuid.uuid4())
        goal = Goal(
            goal_id=goal_id,
            title=title,
            description=description,
            success_criteria=tuple(success_criteria or []),
            session_id=session_id,
            owner_id=owner_id,
            status=GoalStatus.PENDING,
            budget=budget or self._config.default_budget,
            tool_allowlist=tuple(tool_allowlist or []),
            tool_blocklist=tuple(tool_blocklist or []),
            tags=tuple(tags or []),
            deadline=deadline,
            metadata=dict(metadata) if metadata else {},
        )
        self._store.create_goal(goal)
        self._emit(
            KairosEvent(
                kind=KairosEventKind.GOAL_CREATED,
                goal_id=goal_id,
                payload={"title": title},
            )
        )
        logger.info("Goal created: %s (%s)", goal_id, title)
        return goal_id

    async def run(self, goal_id: str) -> AsyncIterator[KairosEvent]:
        """Execute a goal to completion, yielding KairosEvents.

        Manages the full lifecycle:
        1. Transition PENDING → PLANNING
        2. Decompose into a Plan
        3. Execute tasks in dependency order
        4. Checkpoint periodically
        5. Replan on failure (up to config.max_plan_revisions)
        6. Complete or fail the goal

        Yields KairosEvent for every lifecycle change.
        """
        lock = self._get_goal_lock(goal_id)
        async with lock:
            async for event in self._run_goal(goal_id):
                yield event

    async def pause(self, goal_id: str) -> None:
        """Pause a running goal (will stop after current task)."""
        goal = self._store.get_goal(goal_id)
        self._assert_transition(goal, GoalStatus.PAUSED)
        self._store.update_goal_status(goal_id, GoalStatus.PAUSED)
        self._emit(
            KairosEvent(
                kind=KairosEventKind.GOAL_PAUSED,
                goal_id=goal_id,
            )
        )

    async def resume(self, goal_id: str) -> AsyncIterator[KairosEvent]:
        """Resume a paused goal."""
        goal = self._store.get_goal(goal_id)
        self._assert_transition(goal, GoalStatus.ACTIVE)
        async for event in self.run(goal_id):
            yield event

    async def cancel(self, goal_id: str) -> None:
        """Cancel a goal (terminal — cannot be resumed)."""
        goal = self._store.get_goal(goal_id)
        self._assert_transition(goal, GoalStatus.CANCELLED)
        self._store.update_goal_status(
            goal_id, GoalStatus.CANCELLED, completed_at=datetime.now(UTC)
        )
        self._emit(
            KairosEvent(
                kind=KairosEventKind.GOAL_CANCELLED,
                goal_id=goal_id,
            )
        )

    async def resolve_intervention(
        self, goal_id: str, intervention_id: str, response: str
    ) -> None:
        """Provide a response to a pending Intervention."""
        self._store.resolve_intervention(intervention_id, response)
        # If goal is BLOCKED, transition back to ACTIVE
        goal = self._store.get_goal(goal_id)
        if goal.status == GoalStatus.BLOCKED:
            pending = self._store.list_pending_interventions(goal_id)
            if not pending:
                self._store.update_goal_status(goal_id, GoalStatus.ACTIVE)

    def get_goal(self, goal_id: str) -> Goal:
        return self._store.get_goal(goal_id)

    def list_goals(
        self,
        status: GoalStatus | None = None,
        owner_id: str = "",
        limit: int = 100,
    ) -> list[Goal]:
        return self._store.list_goals(status=status, owner_id=owner_id, limit=limit)

    def get_budget_usage(self, goal_id: str) -> BudgetUsage:
        return self._store.get_budget_usage(goal_id)

    async def close(self) -> None:
        """Shut down cleanly."""
        self._store.close()

    # ------------------------------------------------------------------
    # Internal execution loop
    # ------------------------------------------------------------------

    async def _run_goal(self, goal_id: str) -> AsyncIterator[KairosEvent]:
        goal = self._store.get_goal(goal_id)

        # PENDING → PLANNING
        self._store.update_goal_status(
            goal_id, GoalStatus.PLANNING, started_at=datetime.now(UTC)
        )
        event = KairosEvent(
            kind=KairosEventKind.GOAL_STARTED,
            goal_id=goal_id,
            payload={"title": goal.title},
        )
        self._emit(event)
        yield event

        revision = 0

        while revision <= self._config.max_plan_revisions:
            goal = self._store.get_goal(goal_id)

            # Check for pause/cancel
            if goal.status in (GoalStatus.PAUSED, GoalStatus.CANCELLED):
                return

            # --- Planning phase ---
            try:
                plan, tasks = await self._plan(goal, revision)
            except PlanningError as exc:
                logger.error("Planning failed for goal %s: %s", goal_id, exc)
                await self._fail_goal(goal_id, str(exc))
                ev = KairosEvent(
                    kind=KairosEventKind.GOAL_FAILED,
                    goal_id=goal_id,
                    payload={"error": str(exc), "phase": "planning"},
                )
                self._emit(ev)
                yield ev
                return

            plan_ev = KairosEvent(
                kind=KairosEventKind.PLAN_CREATED
                if revision == 0
                else KairosEventKind.PLAN_REVISED,
                goal_id=goal_id,
                plan_id=plan.plan_id,
                payload={"task_count": len(tasks), "revision": revision},
            )
            self._emit(plan_ev)
            yield plan_ev

            # PLANNING → ACTIVE
            self._store.update_goal_status(goal_id, GoalStatus.ACTIVE)

            # --- Task execution phase ---
            completed_ids: list[str] = []
            failed_task: Task | None = None
            failure_context = ""
            task_count = 0

            async for task_event in self._execute_tasks(goal, plan, tasks):
                self._emit(task_event)
                yield task_event

                # Track completion
                if task_event.kind == KairosEventKind.TASK_SUCCEEDED:
                    completed_ids.append(task_event.task_id)
                    task_count += 1

                    # Periodic checkpoint
                    if (
                        self._config.checkpoint_every_n_tasks > 0
                        and task_count % self._config.checkpoint_every_n_tasks == 0
                    ):
                        cp_ev = await self._create_checkpoint(
                            goal,
                            plan,
                            completed_ids,
                            tasks,
                            kind=CheckpointKind.PERIODIC,
                        )
                        self._emit(cp_ev)
                        yield cp_ev

                elif task_event.kind == KairosEventKind.TASK_FAILED:
                    failed_task = await asyncio.get_event_loop().run_in_executor(
                        None, self._store.get_task, task_event.task_id
                    )
                    failure_context = task_event.payload.get("error", "")
                    break

                # Check for budget exceeded during execution
                usage = self._store.get_budget_usage(goal_id)
                exceeded = usage.exceeds(goal.budget)
                if exceeded:
                    budget_ev = KairosEvent(
                        kind=KairosEventKind.BUDGET_EXCEEDED,
                        goal_id=goal_id,
                        payload={"dimension": exceeded},
                    )
                    self._emit(budget_ev)
                    yield budget_ev
                    await self._fail_goal(goal_id, f"Budget exceeded: {exceeded}")
                    fail_ev = KairosEvent(
                        kind=KairosEventKind.GOAL_FAILED,
                        goal_id=goal_id,
                        payload={"error": f"Budget exceeded: {exceeded}"},
                    )
                    self._emit(fail_ev)
                    yield fail_ev
                    return

            # --- Outcome ---
            if failed_task is None:
                # All tasks completed — goal done!
                cp_ev = await self._create_checkpoint(
                    goal,
                    plan,
                    completed_ids,
                    tasks,
                    kind=CheckpointKind.GOAL_COMPLETE,
                )
                self._emit(cp_ev)
                yield cp_ev

                self._store.update_goal_status(
                    goal_id, GoalStatus.COMPLETED, completed_at=datetime.now(UTC)
                )
                self._store.update_plan_status(plan.plan_id, PlanStatus.COMPLETED)
                done_ev = KairosEvent(
                    kind=KairosEventKind.GOAL_COMPLETED,
                    goal_id=goal_id,
                    payload={"tasks_completed": len(completed_ids)},
                )
                self._emit(done_ev)
                yield done_ev
                return

            # Partial failure — try replanning
            revision += 1
            if revision > self._config.max_plan_revisions:
                break

            self._store.update_plan_status(plan.plan_id, PlanStatus.SUPERSEDED)
            cp_ev = await self._create_checkpoint(
                goal,
                plan,
                completed_ids,
                tasks,
                kind=CheckpointKind.FAILURE,
                learnings=failure_context,
            )
            self._emit(cp_ev)
            yield cp_ev

            replan_ev = KairosEvent(
                kind=KairosEventKind.PLAN_REVISED,
                goal_id=goal_id,
                plan_id=plan.plan_id,
                payload={"revision": revision, "reason": failure_context[:200]},
            )
            self._emit(replan_ev)
            yield replan_ev

        # Exhausted revisions
        await self._fail_goal(goal_id, "Max plan revisions exceeded")
        fail_ev = KairosEvent(
            kind=KairosEventKind.GOAL_FAILED,
            goal_id=goal_id,
            payload={"error": "Max plan revisions exceeded"},
        )
        self._emit(fail_ev)
        yield fail_ev

    async def _execute_tasks(
        self, goal: Goal, plan: Plan, tasks: list[Task]
    ) -> AsyncIterator[KairosEvent]:
        """Execute tasks in dependency order, yielding events."""
        completed: set[str] = set()
        pending = list(tasks)

        while pending:
            # Find tasks whose dependencies are all satisfied
            ready = [
                t
                for t in pending
                if all(dep in completed for dep in t.depends_on)
                and t.status == KairosTaskStatus.PENDING
            ]

            if not ready:
                # Deadlock or all remaining tasks are blocked
                logger.warning(
                    "No ready tasks for goal %s — possible dependency deadlock",
                    goal.goal_id,
                )
                break

            # Check for pause/cancel
            current_goal = self._store.get_goal(goal.goal_id)
            if current_goal.status in (GoalStatus.PAUSED, GoalStatus.CANCELLED):
                return

            # Execute ready tasks (sequentially for now — can parallelize later)
            for task in ready:
                self._store.update_task_status(
                    task.task_id,
                    KairosTaskStatus.RUNNING,
                    started_at=datetime.now(UTC),
                )
                start_ev = KairosEvent(
                    kind=KairosEventKind.TASK_STARTED,
                    goal_id=goal.goal_id,
                    plan_id=plan.plan_id,
                    task_id=task.task_id,
                    payload={"title": task.title, "order": task.order_index},
                )
                yield start_ev

                try:
                    result = await self._task_runner.run(task, goal)
                except BudgetExceededError as exc:
                    logger.debug(
                        "suppressed exception in _execute_tasks", exc_info=True
                    )
                    yield KairosEvent(
                        kind=KairosEventKind.BUDGET_EXCEEDED,
                        goal_id=goal.goal_id,
                        task_id=task.task_id,
                        payload={"dimension": exc.dimension},
                    )
                    return
                except InterventionRequiredError as exc:
                    # Transition goal to BLOCKED so status queries show it correctly
                    logger.debug(
                        "suppressed exception in _execute_tasks", exc_info=True
                    )
                    self._store.update_goal_status(goal.goal_id, GoalStatus.BLOCKED)
                    iv_event = KairosEvent(
                        kind=KairosEventKind.INTERVENTION_RAISED,
                        goal_id=goal.goal_id,
                        task_id=task.task_id,
                        payload={"intervention_id": exc.intervention_id},
                    )
                    self._emit(iv_event)
                    yield iv_event
                    # Notify via iMessage so Elliott can respond
                    await self._notify_intervention(goal, exc)
                    return

                self._store.save_task_result(result)
                self._store.update_task_status(
                    task.task_id,
                    result.status,
                    completed_at=datetime.now(UTC),
                )

                if result.status == KairosTaskStatus.SUCCEEDED:
                    completed.add(task.task_id)
                    pending.remove(task)
                    yield KairosEvent(
                        kind=KairosEventKind.TASK_SUCCEEDED,
                        goal_id=goal.goal_id,
                        plan_id=plan.plan_id,
                        task_id=task.task_id,
                        payload={
                            "summary": result.summary,
                            "turns": result.turns_used,
                            "elapsed_ms": result.elapsed_ms,
                        },
                    )
                else:
                    pending.remove(task)
                    yield KairosEvent(
                        kind=KairosEventKind.TASK_FAILED,
                        goal_id=goal.goal_id,
                        plan_id=plan.plan_id,
                        task_id=task.task_id,
                        payload={
                            "error": result.error,
                            "retries": task.retry_count,
                        },
                    )
                    return  # Stop executing — caller handles replanning

    async def _plan(
        self,
        goal: Goal,
        revision: int,
    ) -> tuple[Plan, list[Task]]:
        """Create or revise a plan."""
        engine = self._get_plan_engine()

        if revision == 0:
            plan, tasks = await engine.create_plan(goal)
        else:
            current_plan = self._store.get_active_plan(goal.goal_id)
            if not current_plan:
                plan, tasks = await engine.create_plan(goal)
            else:
                completed_ids = [
                    t.task_id
                    for t in self._store.list_tasks(current_plan.plan_id)
                    if t.status == KairosTaskStatus.SUCCEEDED
                ]
                last_cp = self._store.get_latest_checkpoint(goal.goal_id)
                failure_context = (
                    last_cp.learnings if last_cp else "Previous plan failed"
                )
                plan, tasks = await engine.revise_plan(
                    goal, current_plan, completed_ids, failure_context, revision
                )

        # Persist plan + tasks
        self._store.create_plan(plan)
        for task in tasks:
            self._store.create_task(task)

        # Activate plan
        self._store.update_plan_status(plan.plan_id, PlanStatus.ACTIVE)
        return plan, tasks

    async def _create_checkpoint(
        self,
        goal: Goal,
        plan: Plan,
        completed_task_ids: list[str],
        all_tasks: list[Task],
        *,
        kind: CheckpointKind,
        learnings: str = "",
    ) -> KairosEvent:
        pending_ids = [
            t.task_id for t in all_tasks if t.task_id not in completed_task_ids
        ]
        usage = self._store.get_budget_usage(goal.goal_id)
        cp = Checkpoint(
            checkpoint_id=str(uuid.uuid4()),
            goal_id=goal.goal_id,
            plan_id=plan.plan_id,
            kind=kind,
            completed_task_ids=tuple(completed_task_ids),
            pending_task_ids=tuple(pending_ids),
            summary=f"{len(completed_task_ids)}/{len(all_tasks)} tasks completed",
            learnings=learnings,
            next_steps=f"{len(pending_ids)} tasks remaining",
            budget_usage=usage,
        )
        self._store.create_checkpoint(cp)
        return KairosEvent(
            kind=KairosEventKind.CHECKPOINT_CREATED,
            goal_id=goal.goal_id,
            plan_id=plan.plan_id,
            payload={
                "checkpoint_id": cp.checkpoint_id,
                "kind": kind.value,
                "completed": len(completed_task_ids),
                "pending": len(pending_ids),
            },
        )

    async def _fail_goal(self, goal_id: str, reason: str) -> None:
        self._store.update_goal_status(
            goal_id, GoalStatus.FAILED, completed_at=datetime.now(UTC)
        )
        logger.error("Goal %s failed: %s", goal_id, reason)

    async def _notify_intervention(
        self, goal: "Goal", exc: "InterventionRequiredError"
    ) -> None:
        """Fire-and-forget iMessage alert when a goal blocks on an Intervention."""
        recipient = self._config.notification_recipient
        if not recipient:
            return
        msg = (
            f"⚠ Kairos needs input\n"
            f"Goal: {goal.title}\n"
            f"Task: {exc.task_id[:8] if exc.task_id else '?'}\n"
            f"Reason: {exc}\n"
            f'Reply: obscura kairos respond {goal.goal_id} {exc.intervention_id} "<your answer>"'
        )
        try:
            from obscura.integrations.imessage.client import IMessageClient

            client = IMessageClient(contacts=[recipient])
            await asyncio.wait_for(client.send_message(recipient, msg), timeout=10.0)
            logger.info(
                "Intervention alert sent to %s for goal %s",
                recipient,
                goal.goal_id,
            )
        except Exception:
            logger.warning(
                "Failed to send intervention iMessage alert",
                exc_info=True,
            )

    def _emit(self, event: KairosEvent) -> None:
        """Persist event to the append-only log."""
        try:
            self._store.append_event(event)
        except Exception:
            logger.debug("Failed to persist Kairos event", exc_info=True)

    def _get_plan_engine(self) -> PlanEngine:
        if self._plan_engine is None:
            if self._backend is None:
                raise KairosRuntimeError(
                    "No backend configured for PlanEngine — pass backend= to Kairos()"
                )
            self._plan_engine = PlanEngine(self._backend, self._config)
        return self._plan_engine

    def _get_goal_lock(self, goal_id: str) -> asyncio.Lock:
        if goal_id not in self._goal_locks:
            self._goal_locks[goal_id] = asyncio.Lock()
        return self._goal_locks[goal_id]

    def _assert_transition(self, goal: Goal, target: GoalStatus) -> None:
        allowed = KAIROS_VALID_GOAL_TRANSITIONS.get(goal.status, frozenset())
        if target not in allowed:
            raise GoalStateError(
                f"Cannot transition goal from {goal.status.value} to {target.value}",
                from_state=goal.status.value,
                to_state=target.value,
                goal_id=goal.goal_id,
            )

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def store(self) -> GoalStoreProtocol:
        return self._store

    @property
    def config(self) -> KairosConfig:
        return self._config
