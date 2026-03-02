"""
obscura.core.supervisor.types — Core types for the deterministic supervisor.

Defines supervisor states, events, configuration, and run context.
All types are frozen dataclasses for immutability guarantees.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


# ---------------------------------------------------------------------------
# Supervisor states
# ---------------------------------------------------------------------------


class SupervisorState(enum.Enum):
    """Explicit states in the supervisor state machine.

    Transitions are enforced by ``SessionStateMachine``.
    """

    IDLE = "idle"
    BUILDING_CONTEXT = "building_context"
    RUNNING_MODEL = "running_model"
    RUNNING_TOOLS = "running_tools"
    COMMITTING_MEMORY = "committing_memory"
    FINALIZING = "finalizing"
    FAILED = "failed"


# Valid transitions: source → set of allowed targets
VALID_SUPERVISOR_TRANSITIONS: dict[SupervisorState, frozenset[SupervisorState]] = {
    SupervisorState.IDLE: frozenset({SupervisorState.BUILDING_CONTEXT}),
    SupervisorState.BUILDING_CONTEXT: frozenset(
        {SupervisorState.RUNNING_MODEL, SupervisorState.FAILED}
    ),
    SupervisorState.RUNNING_MODEL: frozenset(
        {
            SupervisorState.RUNNING_TOOLS,
            SupervisorState.COMMITTING_MEMORY,
            SupervisorState.FAILED,
        }
    ),
    SupervisorState.RUNNING_TOOLS: frozenset(
        {SupervisorState.RUNNING_MODEL, SupervisorState.FAILED}
    ),
    SupervisorState.COMMITTING_MEMORY: frozenset(
        {SupervisorState.FINALIZING, SupervisorState.FAILED}
    ),
    SupervisorState.FINALIZING: frozenset({SupervisorState.IDLE}),
    SupervisorState.FAILED: frozenset({SupervisorState.IDLE}),
}


# ---------------------------------------------------------------------------
# Supervisor event kinds
# ---------------------------------------------------------------------------


class SupervisorEventKind(enum.Enum):
    """Events emitted by the supervisor (append-only log)."""

    # State transitions
    STATE_TRANSITION = "state_transition"

    # Lock lifecycle
    LOCK_ACQUIRED = "lock_acquired"
    LOCK_RELEASED = "lock_released"
    LOCK_STOLEN = "lock_stolen"

    # Build phase
    CONTEXT_BUILT = "context_built"
    TOOLS_FROZEN = "tools_frozen"
    MEMORY_RETRIEVED = "memory_retrieved"
    PROMPT_ASSEMBLED = "prompt_assembled"

    # Execution
    MODEL_TURN_START = "model_turn_start"
    MODEL_TURN_END = "model_turn_end"
    TOOL_EXECUTION_START = "tool_execution_start"
    TOOL_EXECUTION_END = "tool_execution_end"

    # Memory
    MEMORY_COMMIT = "memory_commit"
    MEMORY_DEDUPLICATED = "memory_deduplicated"
    MEMORY_GATED = "memory_gated"

    # Hooks (first-class)
    HOOK_REGISTERED = "hook_registered"
    HOOK_FIRED = "hook_fired"
    HOOK_REMOVED = "hook_removed"

    # Heartbeat (first-class)
    HEARTBEAT = "heartbeat"

    # Lifecycle
    RUN_STARTED = "run_started"
    RUN_COMPLETED = "run_completed"
    RUN_FAILED = "run_failed"

    # Observability
    DRIFT_DETECTED = "drift_detected"


# ---------------------------------------------------------------------------
# Hook points
# ---------------------------------------------------------------------------


class SupervisorHookPoint(enum.Enum):
    """Hook points in the supervisor lifecycle."""

    PRE_BUILD_CONTEXT = "pre_build_context"
    POST_BUILD_CONTEXT = "post_build_context"
    PRE_MODEL_TURN = "pre_model_turn"
    POST_MODEL_TURN = "post_model_turn"
    PRE_TOOL_EXECUTION = "pre_tool_execution"
    POST_TOOL_EXECUTION = "post_tool_execution"
    PRE_MEMORY_COMMIT = "pre_memory_commit"
    POST_MEMORY_COMMIT = "post_memory_commit"
    PRE_FINALIZE = "pre_finalize"
    POST_FINALIZE = "post_finalize"
    ON_ERROR = "on_error"
    ON_HEARTBEAT = "on_heartbeat"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SupervisorConfig:
    """Configuration for the Supervisor.

    All timeouts in seconds. All sizes in count.
    """

    # Lock
    lock_timeout: float = 30.0
    lock_ttl: float = 60.0
    lock_heartbeat_interval: float = 5.0

    # Heartbeat
    heartbeat_interval: float = 5.0

    # Execution
    max_turn_duration: float = 300.0
    max_tool_duration: float = 120.0
    max_turns: int = 10

    # Memory
    memory_commit_batch_size: int = 20
    memory_min_importance: float = 0.3
    memory_token_budget: int = 2000

    # Prompt
    prompt_token_budget: int = 0  # 0 = unlimited
    reserved_output_tokens: int = 4096

    # Retry
    max_retries: int = 3
    retry_initial_backoff: float = 1.0
    retry_backoff_factor: float = 2.0
    retry_max_backoff: float = 30.0

    # Observability
    log_prompt_hash: bool = True
    log_tool_snapshot_hash: bool = True
    detect_drift: bool = True


# ---------------------------------------------------------------------------
# Run context (frozen per run)
# ---------------------------------------------------------------------------


def _empty_tuple() -> tuple[str, ...]:
    return ()


def _empty_dict() -> dict[str, Any]:
    return {}


@dataclass(frozen=True)
class RunContext:
    """Immutable context for a single supervisor run.

    Created during BUILDING_CONTEXT, never modified during the run.
    """

    run_id: str
    session_id: str
    prompt_hash: str
    tool_snapshot_hash: str
    tool_names: tuple[str, ...] = field(default_factory=_empty_tuple)
    memory_item_ids: tuple[str, ...] = field(default_factory=_empty_tuple)
    started_at: datetime = field(default_factory=datetime.now)
    metadata: dict[str, Any] = field(default_factory=_empty_dict)


# ---------------------------------------------------------------------------
# Supervisor event record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SupervisorEvent:
    """A single event in the supervisor's append-only log."""

    kind: SupervisorEventKind
    run_id: str = ""
    session_id: str = ""
    payload: dict[str, Any] = field(default_factory=_empty_dict)
    timestamp: datetime = field(default_factory=datetime.now)


# ---------------------------------------------------------------------------
# Prompt snapshot
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PromptSection:
    """A single section of an assembled prompt."""

    name: str
    content: str
    token_estimate: int = 0
    frozen: bool = True


@dataclass(frozen=True)
class PromptSnapshot:
    """Immutable prompt assembled for a run.

    The hash covers all sections in order. If the hash changes
    between turns within a run, that's a drift signal.
    """

    sections: tuple[PromptSection, ...]
    prompt_hash: str
    total_tokens: int = 0
    assembled_at: datetime = field(default_factory=datetime.now)


# ---------------------------------------------------------------------------
# Memory types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MemoryCandidate:
    """A memory item candidate for commit gating."""

    key: str
    content: str
    content_hash: str
    importance: float = 0.5
    relevance: float = 0.0
    recency: float = 1.0
    pinned: bool = False
    source_run_id: str = ""

    @property
    def score(self) -> float:
        """Composite score: importance(0.4) + recency(0.3) + relevance(0.3)."""
        return (
            self.importance * 0.4
            + self.recency * 0.3
            + self.relevance * 0.3
        )


@dataclass(frozen=True)
class MemoryCommitResult:
    """Result of a memory commit operation."""

    committed: int = 0
    deduplicated: int = 0
    gated: int = 0
    errors: int = 0


# ---------------------------------------------------------------------------
# Lock types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LockInfo:
    """Information about a session lock."""

    session_id: str
    holder_id: str
    acquired_at: datetime
    heartbeat_at: datetime
    expires_at: datetime

    @property
    def is_expired(self) -> bool:
        return datetime.now(UTC) >= self.expires_at


# ---------------------------------------------------------------------------
# Heartbeat record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionHeartbeat:
    """A heartbeat emitted during a supervised run.

    First-class citizen: persisted as both a session event and
    a heartbeat record for the lock manager.
    """

    session_id: str
    run_id: str
    seq: int
    state: SupervisorState
    turn_number: int = 0
    elapsed_ms: int = 0
    timestamp: datetime = field(default_factory=datetime.now)
    metadata: dict[str, Any] = field(default_factory=_empty_dict)
