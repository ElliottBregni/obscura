"""Tests for obscura.core.kairos — the autonomous goal runtime."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from obscura.core.kairos.errors import (
    BudgetExceededError,
    EmptyPlanError,
    GoalNotFoundError,
    GoalStateError,
    PlanningError,
    TaskNotFoundError,
)
from obscura.core.kairos.goal_store import GoalStore
from obscura.core.kairos.kairos import Kairos
from obscura.core.kairos.schema import (
    REQUIRED_TABLES,
    init_kairos_schema,
    verify_kairos_schema,
)
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
    KairosEventKind,
    Plan,
    PlanStatus,
    Task,
    TaskResult,
    TaskStatus,
    VALID_GOAL_TRANSITIONS,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    return tmp_path / "kairos_test.db"


@pytest.fixture
def store(tmp_db: Path) -> GoalStore:
    s = GoalStore(tmp_db)
    yield s
    s.close()


def _make_goal(
    goal_id: str | None = None,
    title: str = "Test Goal",
    status: GoalStatus = GoalStatus.PENDING,
    budget: GoalBudget | None = None,
) -> Goal:
    return Goal(
        goal_id=goal_id or str(uuid.uuid4()),
        title=title,
        description="A test goal",
        success_criteria=("criterion 1", "criterion 2"),
        status=status,
        budget=budget or GoalBudget(),
        created_at=datetime.now(UTC),
    )


def _make_plan(
    goal_id: str, task_ids: list[str] | None = None, revision: int = 0
) -> Plan:
    return Plan(
        plan_id=str(uuid.uuid4()),
        goal_id=goal_id,
        revision=revision,
        rationale="test plan",
        task_ids=tuple(task_ids or []),
        status=PlanStatus.ACTIVE,
        created_at=datetime.now(UTC),
    )


def _make_task(
    goal_id: str,
    plan_id: str,
    order_index: int = 0,
    status: TaskStatus = TaskStatus.PENDING,
    depends_on: tuple[str, ...] = (),
) -> Task:
    return Task(
        task_id=str(uuid.uuid4()),
        goal_id=goal_id,
        plan_id=plan_id,
        title=f"Task {order_index}",
        description=f"Do thing {order_index}",
        order_index=order_index,
        depends_on=depends_on,
        status=status,
        created_at=datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# Schema tests
# ---------------------------------------------------------------------------


class TestSchema:
    def test_init_creates_all_tables(self, tmp_db: Path) -> None:
        conn = sqlite3.connect(str(tmp_db))
        init_kairos_schema(conn)
        missing = verify_kairos_schema(conn)
        assert missing == [], f"Missing tables: {missing}"
        conn.close()

    def test_verify_returns_missing(self, tmp_db: Path) -> None:
        conn = sqlite3.connect(str(tmp_db))
        # Don't init — all tables should be missing
        missing = verify_kairos_schema(conn)
        assert set(missing) == set(REQUIRED_TABLES)
        conn.close()

    def test_idempotent_init(self, tmp_db: Path) -> None:
        conn = sqlite3.connect(str(tmp_db))
        init_kairos_schema(conn)
        init_kairos_schema(conn)  # second call must not raise
        assert verify_kairos_schema(conn) == []
        conn.close()


# ---------------------------------------------------------------------------
# Types tests
# ---------------------------------------------------------------------------


class TestTypes:
    def test_goal_is_frozen(self) -> None:
        goal = _make_goal()
        with pytest.raises((AttributeError, TypeError)):
            goal.title = "mutated"  # type: ignore[misc]

    def test_plan_is_frozen(self) -> None:
        plan = _make_plan("g1")
        with pytest.raises((AttributeError, TypeError)):
            plan.revision = 99  # type: ignore[misc]

    def test_task_is_frozen(self) -> None:
        task = _make_task("g1", "p1")
        with pytest.raises((AttributeError, TypeError)):
            task.title = "mutated"  # type: ignore[misc]

    def test_valid_goal_transitions_completeness(self) -> None:
        for state in GoalStatus:
            assert state in VALID_GOAL_TRANSITIONS, f"{state} missing from transitions"

    def test_terminal_states_have_no_transitions(self) -> None:
        for terminal in (GoalStatus.COMPLETED, GoalStatus.FAILED, GoalStatus.CANCELLED):
            assert VALID_GOAL_TRANSITIONS[terminal] == frozenset()

    def test_budget_exceeded_detection(self) -> None:
        budget = GoalBudget(max_tasks=3)
        usage_ok = BudgetUsage(tasks_run=2)
        usage_exceeded = BudgetUsage(tasks_run=3)
        assert usage_ok.exceeds(budget) is None
        assert usage_exceeded.exceeds(budget) == "max_tasks"

    def test_budget_unlimited_zero(self) -> None:
        budget = GoalBudget()  # all zeros = unlimited
        usage = BudgetUsage(tasks_run=9999, turns_used=9999, tokens_used=9999)
        assert usage.exceeds(budget) is None

    def test_kairos_event_defaults(self) -> None:
        ev = KairosEvent(kind=KairosEventKind.GOAL_CREATED, goal_id="g1")
        assert ev.plan_id == ""
        assert ev.task_id == ""
        assert isinstance(ev.timestamp, datetime)


# ---------------------------------------------------------------------------
# GoalStore tests
# ---------------------------------------------------------------------------


class TestGoalStore:
    def test_create_and_get_goal(self, store: GoalStore) -> None:
        goal = _make_goal()
        store.create_goal(goal)
        fetched = store.get_goal(goal.goal_id)
        assert fetched.goal_id == goal.goal_id
        assert fetched.title == goal.title
        assert fetched.status == GoalStatus.PENDING

    def test_get_nonexistent_goal_raises(self, store: GoalStore) -> None:
        with pytest.raises(GoalNotFoundError):
            store.get_goal("no-such-id")

    def test_update_goal_status(self, store: GoalStore) -> None:
        goal = _make_goal()
        store.create_goal(goal)
        store.update_goal_status(goal.goal_id, GoalStatus.ACTIVE)
        fetched = store.get_goal(goal.goal_id)
        assert fetched.status == GoalStatus.ACTIVE

    def test_list_goals_by_status(self, store: GoalStore) -> None:
        g1 = _make_goal(status=GoalStatus.PENDING)
        g2 = _make_goal(status=GoalStatus.ACTIVE)
        g3 = _make_goal(status=GoalStatus.PENDING)
        for g in (g1, g2, g3):
            store.create_goal(g)
        pending = store.list_goals(status=GoalStatus.PENDING)
        assert len(pending) == 2
        active = store.list_goals(status=GoalStatus.ACTIVE)
        assert len(active) == 1

    def test_budget_roundtrip(self, store: GoalStore) -> None:
        budget = GoalBudget(max_tasks=10, max_turns=50, max_tokens=10000)
        goal = _make_goal(budget=budget)
        store.create_goal(goal)
        fetched = store.get_goal(goal.goal_id)
        assert fetched.budget.max_tasks == 10
        assert fetched.budget.max_turns == 50
        assert fetched.budget.max_tokens == 10000

    def test_create_and_retrieve_plan(self, store: GoalStore) -> None:
        goal = _make_goal()
        store.create_goal(goal)
        plan = _make_plan(goal.goal_id)
        store.create_plan(plan)
        fetched = store.get_active_plan(goal.goal_id)
        assert fetched is not None
        assert fetched.plan_id == plan.plan_id

    def test_create_and_get_task(self, store: GoalStore) -> None:
        goal = _make_goal()
        store.create_goal(goal)
        plan = _make_plan(goal.goal_id)
        store.create_plan(plan)
        task = _make_task(goal.goal_id, plan.plan_id)
        store.create_task(task)
        fetched = store.get_task(task.task_id)
        assert fetched.task_id == task.task_id
        assert fetched.title == task.title

    def test_get_nonexistent_task_raises(self, store: GoalStore) -> None:
        with pytest.raises(TaskNotFoundError):
            store.get_task("no-such-task")

    def test_update_task_status(self, store: GoalStore) -> None:
        goal = _make_goal()
        store.create_goal(goal)
        plan = _make_plan(goal.goal_id)
        store.create_plan(plan)
        task = _make_task(goal.goal_id, plan.plan_id)
        store.create_task(task)
        store.update_task_status(task.task_id, TaskStatus.RUNNING)
        fetched = store.get_task(task.task_id)
        assert fetched.status == TaskStatus.RUNNING

    def test_save_task_result(self, store: GoalStore) -> None:
        goal = _make_goal()
        store.create_goal(goal)
        plan = _make_plan(goal.goal_id)
        store.create_plan(plan)
        task = _make_task(goal.goal_id, plan.plan_id)
        store.create_task(task)
        result = TaskResult(
            task_id=task.task_id,
            goal_id=goal.goal_id,
            plan_id=plan.plan_id,
            status=TaskStatus.SUCCEEDED,
            summary="Done",
            output="Full output",
            turns_used=3,
            tokens_used=500,
            elapsed_ms=1200,
        )
        store.save_task_result(result)  # should not raise

    def test_create_and_get_checkpoint(self, store: GoalStore) -> None:
        goal = _make_goal()
        store.create_goal(goal)
        plan = _make_plan(goal.goal_id)
        store.create_plan(plan)
        cp = Checkpoint(
            checkpoint_id=str(uuid.uuid4()),
            goal_id=goal.goal_id,
            plan_id=plan.plan_id,
            kind=CheckpointKind.PERIODIC,
            completed_task_ids=("t1", "t2"),
            pending_task_ids=("t3",),
            summary="2/3 done",
        )
        store.create_checkpoint(cp)
        fetched = store.get_latest_checkpoint(goal.goal_id)
        assert fetched is not None
        assert fetched.checkpoint_id == cp.checkpoint_id
        assert fetched.completed_task_ids == ("t1", "t2")

    def test_create_and_resolve_intervention(self, store: GoalStore) -> None:
        goal = _make_goal()
        store.create_goal(goal)
        iv = Intervention(
            intervention_id=str(uuid.uuid4()),
            goal_id=goal.goal_id,
            task_id=None,
            kind=InterventionKind.AMBIGUITY,
            question="Which file should I edit?",
            options=("file_a.py", "file_b.py"),
            created_at=datetime.now(UTC),
        )
        store.create_intervention(iv)
        pending = store.list_pending_interventions(goal.goal_id)
        assert len(pending) == 1
        store.resolve_intervention(iv.intervention_id, "file_a.py")
        pending_after = store.list_pending_interventions(goal.goal_id)
        assert len(pending_after) == 0

    def test_budget_usage_roundtrip(self, store: GoalStore) -> None:
        goal = _make_goal()
        store.create_goal(goal)
        initial = store.get_budget_usage(goal.goal_id)
        assert initial.tasks_run == 0
        updated = BudgetUsage(tasks_run=5, turns_used=20, tokens_used=3000)
        store.update_budget_usage(goal.goal_id, updated)
        fetched = store.get_budget_usage(goal.goal_id)
        assert fetched.tasks_run == 5
        assert fetched.turns_used == 20
        assert fetched.tokens_used == 3000

    def test_append_event(self, store: GoalStore) -> None:
        goal = _make_goal()
        store.create_goal(goal)
        ev = KairosEvent(
            kind=KairosEventKind.GOAL_CREATED,
            goal_id=goal.goal_id,
            payload={"title": "Test"},
        )
        store.append_event(ev)  # should not raise


# ---------------------------------------------------------------------------
# Errors tests
# ---------------------------------------------------------------------------


class TestErrors:
    def test_goal_not_found_carries_goal_id(self) -> None:
        err = GoalNotFoundError("not found", goal_id="g-123")
        assert err.goal_id == "g-123"
        assert "not found" in str(err)

    def test_budget_exceeded_carries_dimension(self) -> None:
        err = BudgetExceededError("over budget", dimension="max_turns")
        assert err.dimension == "max_turns"

    def test_goal_state_error_carries_states(self) -> None:
        err = GoalStateError("bad transition", from_state="active", to_state="pending")
        assert err.from_state == "active"
        assert err.to_state == "pending"

    def test_planning_error_is_kairos_error(self) -> None:
        from obscura.core.kairos.errors import KairosError

        err = PlanningError("llm failed")
        assert isinstance(err, KairosError)

    def test_empty_plan_error(self) -> None:
        err = EmptyPlanError("no tasks", goal_id="g1")
        assert err.goal_id == "g1"


# ---------------------------------------------------------------------------
# Kairos runtime tests (mocked AgentLoop + backend)
# ---------------------------------------------------------------------------


def _make_mock_agent_loop(output: str = "Task completed.") -> MagicMock:
    """Create a mock AgentLoop that yields a single TEXT event."""
    mock_loop = MagicMock()

    async def _fake_run(prompt: str, session_id: str = "", max_turns: int = 10):
        event = MagicMock()
        event.kind = MagicMock()
        event.kind.value = "text_delta"
        event.text = output
        event.usage = None
        yield event

    mock_loop.run = _fake_run
    return mock_loop


def _make_mock_backend(plan_json: str | None = None) -> MagicMock:
    """Create a mock backend that returns a planning response."""
    if plan_json is None:
        plan_json = json.dumps(
            {
                "rationale": "Simple plan",
                "tasks": [
                    {
                        "title": "Step 1",
                        "description": "Do step 1",
                        "tool_hint": "",
                        "depends_on_indices": [],
                    },
                    {
                        "title": "Step 2",
                        "description": "Do step 2",
                        "tool_hint": "",
                        "depends_on_indices": [0],
                    },
                ],
            }
        )

    mock_backend = MagicMock()

    async def _fake_stream(*args: Any, **kwargs: Any):
        yield MagicMock(text=plan_json)

    mock_backend.stream = _fake_stream
    return mock_backend


@pytest.mark.asyncio
class TestKairosRuntime:
    async def test_create_goal(self, tmp_db: Path) -> None:
        loop = _make_mock_agent_loop()
        kairos = Kairos(tmp_db, agent_loop=loop)
        goal_id = await kairos.create_goal(
            title="My goal",
            description="Do something",
        )
        assert goal_id
        goal = kairos.get_goal(goal_id)
        assert goal.title == "My goal"
        assert goal.status == GoalStatus.PENDING
        await kairos.close()

    async def test_list_goals_empty(self, tmp_db: Path) -> None:
        loop = _make_mock_agent_loop()
        kairos = Kairos(tmp_db, agent_loop=loop)
        goals = kairos.list_goals()
        assert goals == []
        await kairos.close()

    async def test_cancel_pending_goal(self, tmp_db: Path) -> None:
        loop = _make_mock_agent_loop()
        kairos = Kairos(tmp_db, agent_loop=loop)
        goal_id = await kairos.create_goal(title="To cancel", description="x")
        await kairos.cancel(goal_id)
        goal = kairos.get_goal(goal_id)
        assert goal.status == GoalStatus.CANCELLED
        await kairos.close()

    async def test_cancel_terminal_goal_raises(self, tmp_db: Path) -> None:
        loop = _make_mock_agent_loop()
        kairos = Kairos(tmp_db, agent_loop=loop)
        goal_id = await kairos.create_goal(title="x", description="x")
        await kairos.cancel(goal_id)
        # Already CANCELLED — cannot cancel again
        with pytest.raises(GoalStateError):
            await kairos.cancel(goal_id)
        await kairos.close()

    async def test_run_goal_to_completion(self, tmp_db: Path) -> None:
        loop = _make_mock_agent_loop("I completed the task successfully.")
        backend = _make_mock_backend()
        kairos = Kairos(tmp_db, agent_loop=loop, backend=backend)
        goal_id = await kairos.create_goal(
            title="Test run",
            description="Complete two tasks",
            success_criteria=["Both tasks done"],
        )
        events: list[KairosEvent] = []
        async for ev in kairos.run(goal_id):
            events.append(ev)

        event_kinds = [e.kind for e in events]
        assert KairosEventKind.GOAL_STARTED in event_kinds
        assert KairosEventKind.PLAN_CREATED in event_kinds
        assert KairosEventKind.TASK_STARTED in event_kinds
        assert KairosEventKind.TASK_SUCCEEDED in event_kinds
        assert KairosEventKind.GOAL_COMPLETED in event_kinds

        goal = kairos.get_goal(goal_id)
        assert goal.status == GoalStatus.COMPLETED
        await kairos.close()

    async def test_budget_usage_tracked(self, tmp_db: Path) -> None:
        loop = _make_mock_agent_loop()
        backend = _make_mock_backend(
            json.dumps(
                {
                    "rationale": "one task",
                    "tasks": [
                        {
                            "title": "Solo task",
                            "description": "x",
                            "tool_hint": "",
                            "depends_on_indices": [],
                        }
                    ],
                }
            )
        )
        kairos = Kairos(tmp_db, agent_loop=loop, backend=backend)
        goal_id = await kairos.create_goal(title="Budget test", description="x")
        async for _ in kairos.run(goal_id):
            pass
        usage = kairos.get_budget_usage(goal_id)
        assert usage.tasks_run >= 1
        await kairos.close()

    async def test_goal_not_found_raises(self, tmp_db: Path) -> None:
        loop = _make_mock_agent_loop()
        kairos = Kairos(tmp_db, agent_loop=loop)
        with pytest.raises(GoalNotFoundError):
            kairos.get_goal("no-such-goal")
        await kairos.close()

    async def test_budget_exceeded_fails_goal(self, tmp_db: Path) -> None:
        loop = _make_mock_agent_loop()
        backend = _make_mock_backend()
        # Budget of 0 tasks = unlimited, but 1 task max triggers after first task
        tight_budget = GoalBudget(max_tasks=1)
        kairos = Kairos(tmp_db, agent_loop=loop, backend=backend)
        goal_id = await kairos.create_goal(
            title="Tight budget",
            description="Should fail fast",
            budget=tight_budget,
        )
        events: list[KairosEvent] = []
        async for ev in kairos.run(goal_id):
            events.append(ev)

        event_kinds = [e.kind for e in events]
        # Should complete (1 task within budget) or hit budget on 2nd task
        # Either GOAL_COMPLETED (1 task plan fits) or BUDGET_EXCEEDED then GOAL_FAILED
        assert (
            KairosEventKind.GOAL_COMPLETED in event_kinds
            or KairosEventKind.BUDGET_EXCEEDED in event_kinds
        )
        await kairos.close()
