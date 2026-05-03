"""obscura.core.kairos.types — Core types for the Kairos autonomous goal runtime.

Defines the full hierarchy: Goal → Plan → Task → Checkpoint → Intervention.

All types are frozen dataclasses for immutability guarantees.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class GoalStatus(enum.Enum):
    """Lifecycle state of a Goal."""

    PENDING = "pending"  # Created, not yet started
    PLANNING = "planning"  # Decomposing into a Plan
    ACTIVE = "active"  # Has an active Plan being executed
    PAUSED = "paused"  # Execution suspended (user or system)
    BLOCKED = "blocked"  # Waiting on Intervention
    COMPLETED = "completed"  # All success criteria met
    FAILED = "failed"  # Unrecoverable failure
    CANCELLED = "cancelled"  # User-cancelled


class PlanStatus(enum.Enum):
    """Lifecycle state of a Plan."""

    DRAFT = "draft"  # Under construction
    ACTIVE = "active"  # Being executed
    SUPERSEDED = "superseded"  # Replaced by a revised plan
    COMPLETED = "completed"
    FAILED = "failed"


class TaskStatus(enum.Enum):
    """Lifecycle state of a Task."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RETRYING = "retrying"
    BLOCKED = "blocked"  # Waiting on Intervention
    APPROVAL_REQUIRED = "approval_required"
    SKIPPED = "skipped"


class CheckpointKind(enum.Enum):
    """What triggered a Checkpoint."""

    TASK_COMPLETE = "task_complete"
    PLAN_REVISED = "plan_revised"
    INTERVENTION = "intervention"
    PERIODIC = "periodic"
    GOAL_COMPLETE = "goal_complete"
    FAILURE = "failure"


class InterventionKind(enum.Enum):
    """Why human input is required."""

    AMBIGUITY = "ambiguity"  # Goal or task unclear
    RISK = "risk"  # Action is risky / irreversible
    AUTHORIZATION = "authorization"  # Permission scope exceeded
    BUDGET_EXCEEDED = "budget_exceeded"  # Token/time/cost over limit
    APPROVAL = "approval"  # Explicit approval requested
    CLARIFICATION = "clarification"  # Agent needs more info


class KairosEventKind(enum.Enum):
    """Events emitted by the Kairos runtime (append-only log)."""

    # Goal lifecycle
    GOAL_CREATED = "goal_created"
    GOAL_STARTED = "goal_started"
    GOAL_PAUSED = "goal_paused"
    GOAL_RESUMED = "goal_resumed"
    GOAL_COMPLETED = "goal_completed"
    GOAL_FAILED = "goal_failed"
    GOAL_CANCELLED = "goal_cancelled"

    # Plan lifecycle
    PLAN_CREATED = "plan_created"
    PLAN_REVISED = "plan_revised"
    PLAN_COMPLETED = "plan_completed"

    # Task lifecycle
    TASK_STARTED = "task_started"
    TASK_SUCCEEDED = "task_succeeded"
    TASK_FAILED = "task_failed"
    TASK_RETRYING = "task_retrying"
    TASK_BLOCKED = "task_blocked"

    # Checkpoint
    CHECKPOINT_CREATED = "checkpoint_created"

    # Intervention
    INTERVENTION_RAISED = "intervention_raised"
    INTERVENTION_RESOLVED = "intervention_resolved"

    # Budget
    BUDGET_WARNING = "budget_warning"
    BUDGET_EXCEEDED = "budget_exceeded"

    # Heartbeat
    HEARTBEAT = "heartbeat"


# ---------------------------------------------------------------------------
# Valid goal state transitions
# ---------------------------------------------------------------------------

VALID_GOAL_TRANSITIONS: dict[GoalStatus, frozenset[GoalStatus]] = {
    GoalStatus.PENDING: frozenset({GoalStatus.PLANNING, GoalStatus.CANCELLED}),
    GoalStatus.PLANNING: frozenset(
        {GoalStatus.ACTIVE, GoalStatus.FAILED, GoalStatus.CANCELLED}
    ),
    GoalStatus.ACTIVE: frozenset(
        {
            GoalStatus.PAUSED,
            GoalStatus.BLOCKED,
            GoalStatus.COMPLETED,
            GoalStatus.FAILED,
            GoalStatus.CANCELLED,
        }
    ),
    GoalStatus.PAUSED: frozenset({GoalStatus.ACTIVE, GoalStatus.CANCELLED}),
    GoalStatus.BLOCKED: frozenset(
        {GoalStatus.ACTIVE, GoalStatus.FAILED, GoalStatus.CANCELLED}
    ),
    GoalStatus.COMPLETED: frozenset(),
    GoalStatus.FAILED: frozenset(),
    GoalStatus.CANCELLED: frozenset(),
}


# ---------------------------------------------------------------------------
# Helper factories for frozen dataclass defaults
# ---------------------------------------------------------------------------


def _empty_tuple() -> tuple[str, ...]:
    return ()


def _empty_dict() -> dict[str, Any]:
    return {}


def _now() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GoalBudget:
    """Execution budget for a Goal.

    Zero means unlimited.
    """

    max_tasks: int = 0  # 0 = unlimited
    max_turns: int = 0  # Total model turns across all tasks
    max_wall_seconds: float = 0.0  # Wall-clock deadline
    max_tokens: int = 0  # Approximate token budget
    max_retries_per_task: int = 3


@dataclass(frozen=True)
class BudgetUsage:
    """Tracked budget consumption for a Goal."""

    tasks_run: int = 0
    turns_used: int = 0
    elapsed_seconds: float = 0.0
    tokens_used: int = 0
    retries_used: int = 0

    def exceeds(self, budget: GoalBudget) -> str | None:
        """Return first exceeded budget dimension, or None."""
        if budget.max_tasks and self.tasks_run >= budget.max_tasks:
            return "max_tasks"
        if budget.max_turns and self.turns_used >= budget.max_turns:
            return "max_turns"
        if budget.max_wall_seconds and self.elapsed_seconds >= budget.max_wall_seconds:
            return "max_wall_seconds"
        if budget.max_tokens and self.tokens_used >= budget.max_tokens:
            return "max_tokens"
        return None


# ---------------------------------------------------------------------------
# Kairos configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KairosConfig:
    """Configuration for the Kairos runtime."""

    # Planning
    max_plan_tasks: int = 20  # Max tasks in a single plan
    max_plan_revisions: int = 5  # How many times a plan can be revised
    default_model: str = "copilot"  # Model for planning + task execution

    # Execution
    task_timeout_seconds: float = 300.0
    planning_timeout_seconds: float = 60.0

    # Checkpointing
    checkpoint_every_n_tasks: int = 3  # Auto-checkpoint interval
    persist_checkpoints: bool = True

    # Intervention
    auto_pause_on_risk: bool = True  # Pause goal if RISK intervention raised
    max_pending_interventions: int = 5

    # Heartbeat
    heartbeat_interval: float = 10.0

    # Budget defaults (applied to goals without explicit budgets)
    default_budget: GoalBudget = field(default_factory=GoalBudget)


# ---------------------------------------------------------------------------
# Core domain types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Goal:
    """A user-defined outcome with success criteria, constraints, and scope.

    Immutable after creation. Status changes create new ``GoalStatus`` events.
    """

    goal_id: str
    title: str
    description: str
    success_criteria: tuple[str, ...] = field(default_factory=_empty_tuple)

    # Execution context
    session_id: str = ""  # Originating session (optional)
    owner_id: str = ""  # User who created the goal

    # Current state (mutable via event log, not in-place)
    status: GoalStatus = GoalStatus.PENDING

    # Budget + permissions
    budget: GoalBudget = field(default_factory=GoalBudget)
    tool_allowlist: tuple[str, ...] = field(default_factory=_empty_tuple)  # empty = all
    tool_blocklist: tuple[str, ...] = field(default_factory=_empty_tuple)

    # Timing
    created_at: datetime = field(default_factory=_now)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    deadline: datetime | None = None

    # Metadata
    metadata: dict[str, Any] = field(default_factory=_empty_dict)
    tags: tuple[str, ...] = field(default_factory=_empty_tuple)


@dataclass(frozen=True)
class Task:
    """An atomic executable step within a Plan.

    Immutable after creation. Results recorded in TaskResult.
    """

    task_id: str
    goal_id: str
    plan_id: str
    title: str
    description: str
    order_index: int = 0

    # Dependencies
    depends_on: tuple[str, ...] = field(default_factory=_empty_tuple)  # task_ids

    # Execution config
    tool_hint: str = ""  # Preferred tool or tool category
    model: str = ""  # Override model for this task (empty = inherit)
    max_retries: int = 3

    # State
    status: TaskStatus = TaskStatus.PENDING
    retry_count: int = 0

    # Timing
    created_at: datetime = field(default_factory=_now)
    started_at: datetime | None = None
    completed_at: datetime | None = None

    metadata: dict[str, Any] = field(default_factory=_empty_dict)


@dataclass(frozen=True)
class TaskResult:
    """The outcome of a completed Task execution."""

    task_id: str
    goal_id: str
    plan_id: str
    status: TaskStatus
    summary: str = ""
    output: str = ""
    error: str = ""
    turns_used: int = 0
    tokens_used: int = 0
    elapsed_ms: int = 0
    completed_at: datetime = field(default_factory=_now)


@dataclass(frozen=True)
class Plan:
    """A revisable hypothesis for reaching a Goal.

    Contains an ordered list of task_ids. When replanning occurs,
    the old plan is SUPERSEDED and a new one created.
    """

    plan_id: str
    goal_id: str
    revision: int = 0  # Increments on each replan
    rationale: str = ""  # Why this plan was created/revised
    task_ids: tuple[str, ...] = field(default_factory=_empty_tuple)
    status: PlanStatus = PlanStatus.DRAFT
    created_at: datetime = field(default_factory=_now)
    completed_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=_empty_dict)


@dataclass(frozen=True)
class Checkpoint:
    """A snapshot of progress at a point in time.

    Survives restarts. Contains enough context to resume.
    """

    checkpoint_id: str
    goal_id: str
    plan_id: str
    kind: CheckpointKind
    completed_task_ids: tuple[str, ...] = field(default_factory=_empty_tuple)
    pending_task_ids: tuple[str, ...] = field(default_factory=_empty_tuple)
    summary: str = ""  # LLM-generated progress summary
    learnings: str = ""  # New facts discovered during execution
    next_steps: str = ""  # Recommended next actions
    budget_usage: BudgetUsage = field(default_factory=BudgetUsage)
    created_at: datetime = field(default_factory=_now)
    metadata: dict[str, Any] = field(default_factory=_empty_dict)


@dataclass(frozen=True)
class Intervention:
    """A point where human input is required.

    Raised when the agent cannot proceed autonomously.
    Blocks the Goal until resolved.
    """

    intervention_id: str
    goal_id: str
    task_id: str | None  # Which task triggered it (if any)
    kind: InterventionKind
    question: str  # What the agent is asking
    context: str = ""  # Additional context for the user
    options: tuple[str, ...] = field(default_factory=_empty_tuple)  # Suggested answers
    response: str | None = None  # User's response (None if unresolved)
    resolved: bool = False
    created_at: datetime = field(default_factory=_now)
    resolved_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=_empty_dict)


# ---------------------------------------------------------------------------
# Runtime event record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KairosEvent:
    """A single event in the Kairos append-only event log."""

    kind: KairosEventKind
    goal_id: str = ""
    plan_id: str = ""
    task_id: str = ""
    payload: dict[str, Any] = field(default_factory=_empty_dict)
    timestamp: datetime = field(default_factory=_now)


# ---------------------------------------------------------------------------
# Goal run context (snapshot at start of each execution tick)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GoalRunContext:
    """Immutable snapshot of a goal's execution context for one tick."""

    goal_id: str
    plan_id: str
    current_task_id: str
    turn_number: int = 0
    budget_usage: BudgetUsage = field(default_factory=BudgetUsage)
    started_at: datetime = field(default_factory=_now)
    metadata: dict[str, Any] = field(default_factory=_empty_dict)
