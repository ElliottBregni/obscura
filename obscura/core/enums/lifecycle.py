"""Lifecycle / status enums for the obscura runtime.

Every status enum in the runtime lives here, conforming to the
`Lifecycle` Protocol from `_base`. Each is a `StrEnum` whose values match
today's persisted wire strings byte-for-byte to keep `events.db`,
worktree manifests, approval payloads, and other on-disk state working
without migration.
"""

from __future__ import annotations

from enum import StrEnum


# ---------------------------------------------------------------------------
# Session (event_store)
# ---------------------------------------------------------------------------


class SessionStatus(StrEnum):
    """Lifecycle states for a durable agent session."""

    RUNNING = "running"
    WAITING_FOR_TOOL = "waiting_for_tool"
    WAITING_FOR_USER = "waiting_for_user"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"

    def is_terminal(self) -> bool:
        return self in {SessionStatus.COMPLETED, SessionStatus.FAILED}

    def is_active(self) -> bool:
        # PAUSED is suspended, not making progress; the rest are mid-flight.
        return self in {
            SessionStatus.RUNNING,
            SessionStatus.WAITING_FOR_TOOL,
            SessionStatus.WAITING_FOR_USER,
        }


SESSION_VALID_TRANSITIONS: dict[SessionStatus, frozenset[SessionStatus]] = {
    SessionStatus.RUNNING: frozenset(
        {
            SessionStatus.WAITING_FOR_TOOL,
            SessionStatus.WAITING_FOR_USER,
            SessionStatus.PAUSED,
            SessionStatus.COMPLETED,
            SessionStatus.FAILED,
        },
    ),
    SessionStatus.WAITING_FOR_TOOL: frozenset(
        {
            SessionStatus.RUNNING,
            SessionStatus.PAUSED,
            SessionStatus.FAILED,
        },
    ),
    SessionStatus.WAITING_FOR_USER: frozenset(
        {
            SessionStatus.RUNNING,
            SessionStatus.PAUSED,
            SessionStatus.FAILED,
        },
    ),
    SessionStatus.PAUSED: frozenset(
        {
            SessionStatus.RUNNING,
            SessionStatus.FAILED,
        },
    ),
    SessionStatus.COMPLETED: frozenset(),
    SessionStatus.FAILED: frozenset(),
}


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------


class CircuitState(StrEnum):
    """State of a per-backend circuit breaker."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

    def is_terminal(self) -> bool:
        # Circuit breakers oscillate; no member is permanent.
        return False

    def is_active(self) -> bool:
        # CLOSED = normal traffic; HALF_OPEN = probing. OPEN rejects.
        return self in {CircuitState.CLOSED, CircuitState.HALF_OPEN}


# ---------------------------------------------------------------------------
# Tool approvals
# ---------------------------------------------------------------------------


class ApprovalStatus(StrEnum):
    """Lifecycle of a user-confirmation request for a tool call."""

    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"

    def is_terminal(self) -> bool:
        # EXPIRED is terminal — the original request can't be acted on.
        return self in {
            ApprovalStatus.APPROVED,
            ApprovalStatus.DENIED,
            ApprovalStatus.EXPIRED,
        }

    def is_active(self) -> bool:
        return self is ApprovalStatus.PENDING


# ---------------------------------------------------------------------------
# Background tasks (shell process manager)
# ---------------------------------------------------------------------------


class BackgroundTaskStatus(StrEnum):
    """Lifecycle of a long-running background shell task."""

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    STOPPED = "stopped"

    def is_terminal(self) -> bool:
        return self in {
            BackgroundTaskStatus.COMPLETED,
            BackgroundTaskStatus.FAILED,
            BackgroundTaskStatus.STOPPED,
        }

    def is_active(self) -> bool:
        return self is BackgroundTaskStatus.RUNNING


# ---------------------------------------------------------------------------
# Worktree registry
# ---------------------------------------------------------------------------


class WorktreeStatus(StrEnum):
    """Lifecycle of a registered git worktree checkout."""

    ACTIVE = "active"
    ORPHAN = "orphan"
    KEPT = "kept"

    def is_terminal(self) -> bool:
        # KEPT means the user opted to retain the checkout — no further
        # registry transitions expected. ORPHAN is a candidate for cleanup.
        return self is WorktreeStatus.KEPT

    def is_active(self) -> bool:
        return self is WorktreeStatus.ACTIVE


# ---------------------------------------------------------------------------
# Task queue (SQLite-backed work queue)
# ---------------------------------------------------------------------------


class TaskQueueStatus(StrEnum):
    """SQL-level lifecycle of a row in the durable task queue."""

    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"

    def is_terminal(self) -> bool:
        return self in {TaskQueueStatus.COMPLETED, TaskQueueStatus.FAILED}

    def is_active(self) -> bool:
        return self is TaskQueueStatus.PENDING


# ---------------------------------------------------------------------------
# Startup health checks (core/health.py)
# ---------------------------------------------------------------------------


class HealthStatus(StrEnum):
    """Status of an optional dependency at startup."""

    OK = "ok"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"

    def is_terminal(self) -> bool:
        # Health checks are point-in-time; nothing here is "permanent".
        return False

    def is_active(self) -> bool:
        return self is HealthStatus.OK


# ---------------------------------------------------------------------------
# Heartbeat (agent runtime health)
# ---------------------------------------------------------------------------


class AgentHealthStatus(StrEnum):
    """Health status reported by an agent's heartbeat client."""

    HEALTHY = "healthy"
    WARNING = "warning"
    CRITICAL = "critical"
    UNKNOWN = "unknown"

    def is_terminal(self) -> bool:
        # Heartbeats are continuous; no member is terminal.
        return False

    def is_active(self) -> bool:
        # HEALTHY and WARNING agents still get traffic; CRITICAL/UNKNOWN do not.
        return self in {AgentHealthStatus.HEALTHY, AgentHealthStatus.WARNING}


# ---------------------------------------------------------------------------
# Kairos goals / plans / tasks
# ---------------------------------------------------------------------------


class GoalStatus(StrEnum):
    """Lifecycle state of a Kairos Goal."""

    PENDING = "pending"
    PLANNING = "planning"
    ACTIVE = "active"
    PAUSED = "paused"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    def is_terminal(self) -> bool:
        return self in {
            GoalStatus.COMPLETED,
            GoalStatus.FAILED,
            GoalStatus.CANCELLED,
        }

    def is_active(self) -> bool:
        # PLANNING and ACTIVE are progressing; PAUSED/BLOCKED are suspended.
        return self in {GoalStatus.PLANNING, GoalStatus.ACTIVE}


class PlanStatus(StrEnum):
    """Lifecycle state of a Kairos Plan."""

    DRAFT = "draft"
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    COMPLETED = "completed"
    FAILED = "failed"

    def is_terminal(self) -> bool:
        # SUPERSEDED is terminal: the plan was replaced and won't execute.
        return self in {
            PlanStatus.SUPERSEDED,
            PlanStatus.COMPLETED,
            PlanStatus.FAILED,
        }

    def is_active(self) -> bool:
        return self is PlanStatus.ACTIVE


class KairosTaskStatus(StrEnum):
    """Lifecycle state of a Kairos Task."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RETRYING = "retrying"
    BLOCKED = "blocked"
    APPROVAL_REQUIRED = "approval_required"
    SKIPPED = "skipped"

    def is_terminal(self) -> bool:
        # SKIPPED counts as terminal — the task isn't going to run.
        return self in {
            KairosTaskStatus.SUCCEEDED,
            KairosTaskStatus.FAILED,
            KairosTaskStatus.SKIPPED,
        }

    def is_active(self) -> bool:
        # RETRYING is mid-flight (pending re-execution); BLOCKED/APPROVAL_REQUIRED are suspended.
        return self in {KairosTaskStatus.RUNNING, KairosTaskStatus.RETRYING}


# Kairos-specific transition map keyed by GoalStatus.
KAIROS_VALID_GOAL_TRANSITIONS: dict[GoalStatus, frozenset[GoalStatus]] = {
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
# Supervisor state machine
# ---------------------------------------------------------------------------


class SupervisorState(StrEnum):
    """Explicit states in the deterministic supervisor state machine."""

    IDLE = "idle"
    BUILDING_CONTEXT = "building_context"
    RUNNING_MODEL = "running_model"
    RUNNING_TOOLS = "running_tools"
    COMMITTING_MEMORY = "committing_memory"
    FINALIZING = "finalizing"
    FAILED = "failed"
    EVAL_FAILED = "eval_failed"

    def is_terminal(self) -> bool:
        # FAILED/EVAL_FAILED transition back to IDLE; nothing in the machine
        # is truly terminal (the run ends, not the enum).
        return False

    def is_active(self) -> bool:
        # IDLE is the resting state; the failure states are post-mortem.
        return self in {
            SupervisorState.BUILDING_CONTEXT,
            SupervisorState.RUNNING_MODEL,
            SupervisorState.RUNNING_TOOLS,
            SupervisorState.COMMITTING_MEMORY,
            SupervisorState.FINALIZING,
        }


SUPERVISOR_VALID_TRANSITIONS: dict[SupervisorState, frozenset[SupervisorState]] = {
    SupervisorState.IDLE: frozenset({SupervisorState.BUILDING_CONTEXT}),
    SupervisorState.BUILDING_CONTEXT: frozenset(
        {SupervisorState.RUNNING_MODEL, SupervisorState.FAILED},
    ),
    SupervisorState.RUNNING_MODEL: frozenset(
        {
            SupervisorState.RUNNING_TOOLS,
            SupervisorState.COMMITTING_MEMORY,
            SupervisorState.FAILED,
        },
    ),
    SupervisorState.RUNNING_TOOLS: frozenset(
        {
            SupervisorState.RUNNING_MODEL,
            SupervisorState.COMMITTING_MEMORY,
            SupervisorState.FAILED,
        },
    ),
    SupervisorState.COMMITTING_MEMORY: frozenset(
        {
            SupervisorState.FINALIZING,
            SupervisorState.FAILED,
            SupervisorState.EVAL_FAILED,
        },
    ),
    SupervisorState.FINALIZING: frozenset({SupervisorState.IDLE}),
    SupervisorState.FAILED: frozenset({SupervisorState.IDLE}),
    SupervisorState.EVAL_FAILED: frozenset({SupervisorState.IDLE}),
}


# ---------------------------------------------------------------------------
# Lazy plugin loader
# ---------------------------------------------------------------------------


class LazyState(StrEnum):
    """Lazy-load lifecycle of an Obscura plugin."""

    DISCOVERED = "discovered"
    READY = "ready"
    INITIALIZING = "initializing"
    ACTIVE = "active"
    SUSPENDED = "suspended"
    FAILED = "failed"

    def is_terminal(self) -> bool:
        # FAILED is terminal — the plugin won't transition without an explicit
        # re-register. SUSPENDED can still be resumed.
        return self is LazyState.FAILED

    def is_active(self) -> bool:
        return self is LazyState.ACTIVE


# ---------------------------------------------------------------------------
# Parity (backend feature support)
# ---------------------------------------------------------------------------


class FeatureStatus(StrEnum):
    """Parity status for one backend feature."""

    SUPPORTED = "supported"
    PARTIAL = "partial"
    UNSUPPORTED = "unsupported"

    def is_terminal(self) -> bool:
        # Declarative status, not a lifecycle — nothing transitions.
        return False

    def is_active(self) -> bool:
        # SUPPORTED and PARTIAL count as usable; UNSUPPORTED does not.
        return self in {FeatureStatus.SUPPORTED, FeatureStatus.PARTIAL}


__all__ = [
    "AgentHealthStatus",
    "ApprovalStatus",
    "BackgroundTaskStatus",
    "CircuitState",
    "FeatureStatus",
    "GoalStatus",
    "HealthStatus",
    "KAIROS_VALID_GOAL_TRANSITIONS",
    "KairosTaskStatus",
    "LazyState",
    "PlanStatus",
    "SESSION_VALID_TRANSITIONS",
    "SUPERVISOR_VALID_TRANSITIONS",
    "SessionStatus",
    "SupervisorState",
    "TaskQueueStatus",
    "WorktreeStatus",
]
