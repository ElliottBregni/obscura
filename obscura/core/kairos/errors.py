"""obscura.core.kairos.errors — Exception hierarchy for the Kairos runtime."""

from __future__ import annotations

from obscura.core.enums.error import ErrorCategory


class KairosError(Exception):
    """Base class for all Kairos errors."""

    category: ErrorCategory = ErrorCategory.KAIROS_RUNTIME

    def __init__(self, message: str, goal_id: str = "", task_id: str = "") -> None:
        super().__init__(message)
        self.goal_id = goal_id
        self.task_id = task_id


# ---------------------------------------------------------------------------
# Goal errors
# ---------------------------------------------------------------------------


class GoalNotFoundError(KairosError):
    """Goal ID does not exist in the store."""

    category = ErrorCategory.KAIROS_GOAL


class GoalStateError(KairosError):
    """Invalid goal state transition attempted."""

    category = ErrorCategory.KAIROS_GOAL

    def __init__(
        self, message: str, from_state: str = "", to_state: str = "", **kwargs: str
    ) -> None:
        super().__init__(message, **kwargs)
        self.from_state = from_state
        self.to_state = to_state


class GoalAlreadyActiveError(KairosError):
    """Goal is already running — cannot start a second instance."""

    category = ErrorCategory.KAIROS_GOAL


# ---------------------------------------------------------------------------
# Plan errors
# ---------------------------------------------------------------------------


class PlanningError(KairosError):
    """Failed to decompose a goal into a plan."""

    category = ErrorCategory.KAIROS_PLAN


class PlanRevisionLimitError(KairosError):
    """Maximum plan revisions exceeded."""

    category = ErrorCategory.KAIROS_PLAN


class EmptyPlanError(KairosError):
    """Planner returned zero tasks."""

    category = ErrorCategory.KAIROS_PLAN


# ---------------------------------------------------------------------------
# Task errors
# ---------------------------------------------------------------------------


class TaskNotFoundError(KairosError):
    """Task ID does not exist."""

    category = ErrorCategory.KAIROS_TASK


class TaskExecutionError(KairosError):
    """Task failed during execution."""

    category = ErrorCategory.KAIROS_TASK

    def __init__(self, message: str, retryable: bool = True, **kwargs: str) -> None:
        super().__init__(message, **kwargs)
        self.retryable = retryable


class TaskRetryLimitError(KairosError):
    """Task exceeded its retry limit."""

    category = ErrorCategory.KAIROS_TASK


class TaskDependencyError(KairosError):
    """A task dependency failed, blocking execution."""

    category = ErrorCategory.KAIROS_TASK


# ---------------------------------------------------------------------------
# Intervention errors
# ---------------------------------------------------------------------------


class InterventionRequiredError(KairosError):
    """Execution cannot proceed — human input required."""

    category = ErrorCategory.KAIROS_INTERVENTION

    def __init__(
        self,
        message: str,
        intervention_id: str = "",
        **kwargs: str,
    ) -> None:
        super().__init__(message, **kwargs)
        self.intervention_id = intervention_id


class InterventionNotFoundError(KairosError):
    """Intervention ID does not exist."""

    category = ErrorCategory.KAIROS_INTERVENTION


# ---------------------------------------------------------------------------
# Budget errors
# ---------------------------------------------------------------------------


class BudgetExceededError(KairosError):
    """Goal execution exceeded its budget."""

    category = ErrorCategory.KAIROS_BUDGET

    def __init__(self, message: str, dimension: str = "", **kwargs: str) -> None:
        super().__init__(message, **kwargs)
        self.dimension = dimension  # e.g. "max_turns", "max_tokens"


# ---------------------------------------------------------------------------
# Runtime errors
# ---------------------------------------------------------------------------


class KairosRuntimeError(KairosError):
    """Internal Kairos runtime error."""

    category = ErrorCategory.KAIROS_RUNTIME


class CheckpointError(KairosError):
    """Failed to create or restore a checkpoint."""

    category = ErrorCategory.KAIROS_RUNTIME
