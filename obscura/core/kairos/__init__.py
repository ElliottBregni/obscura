"""obscura.core.kairos — Autonomous goal runtime for Obscura.

Provides the Kairos runtime: a goal-seeking execution engine that
decomposes user-defined Goals into Plans, executes Tasks autonomously,
checkpoints progress, and escalates to humans via Interventions only
when truly needed.

Hierarchy::

    Goal → Plan → Task → Checkpoint → Intervention (if needed)

Quick start::

    from obscura.core.kairos import Kairos, KairosConfig
    from obscura.core.kairos.types import GoalBudget

    kairos = Kairos(
        db_path="~/.obscura/kairos.db",
        agent_loop=my_agent_loop,
        backend=my_backend,
    )

    goal_id = await kairos.create_goal(
        title="Audit the codebase for security issues",
        description="Check all Python files for common vulnerabilities",
        success_criteria=["All files scanned", "Report generated"],
        budget=GoalBudget(max_turns=100, max_wall_seconds=1800),
    )

    async for event in kairos.run(goal_id):
        print(event.kind.value, event.payload)

    await kairos.close()
"""

from __future__ import annotations

from obscura.core.kairos.errors import (
    BudgetExceededError,
    CheckpointError,
    EmptyPlanError,
    ErrorCategory,
    GoalAlreadyActiveError,
    GoalNotFoundError,
    GoalStateError,
    InterventionNotFoundError,
    InterventionRequiredError,
    KairosError,
    KairosRuntimeError,
    PlanRevisionLimitError,
    PlanningError,
    TaskDependencyError,
    TaskExecutionError,
    TaskNotFoundError,
    TaskRetryLimitError,
)
from obscura.core.kairos.goal_store import GoalStore
from obscura.core.kairos.kairos import Kairos
from obscura.core.kairos.plan_engine import PlanEngine
from obscura.core.kairos.schema import (
    REQUIRED_TABLES,
    init_kairos_schema,
    verify_kairos_schema,
)
from obscura.core.kairos.task_runner import TaskRunner
from obscura.core.kairos.types import (
    BudgetUsage,
    Checkpoint,
    CheckpointKind,
    Goal,
    GoalBudget,
    GoalRunContext,
    Intervention,
    InterventionKind,
    KairosConfig,
    KairosEvent,
    KairosEventKind,
    Plan,
    Task,
    TaskResult,
)

__all__ = [
    # Main runtime
    "Kairos",
    # Config
    "KairosConfig",
    # Budget
    "GoalBudget",
    "BudgetUsage",
    # Domain types
    "Goal",
    "GoalRunContext",
    "Plan",
    "Task",
    "TaskResult",
    "Checkpoint",
    "CheckpointKind",
    "Intervention",
    "InterventionKind",
    # Events
    "KairosEvent",
    "KairosEventKind",
    # Components (for advanced use)
    "GoalStore",
    "PlanEngine",
    "TaskRunner",
    # Schema
    "REQUIRED_TABLES",
    "init_kairos_schema",
    "verify_kairos_schema",
    # Errors
    "BudgetExceededError",
    "CheckpointError",
    "EmptyPlanError",
    "ErrorCategory",
    "GoalAlreadyActiveError",
    "GoalNotFoundError",
    "GoalStateError",
    "InterventionNotFoundError",
    "InterventionRequiredError",
    "KairosError",
    "KairosRuntimeError",
    "PlanRevisionLimitError",
    "PlanningError",
    "TaskDependencyError",
    "TaskExecutionError",
    "TaskNotFoundError",
    "TaskRetryLimitError",
]
