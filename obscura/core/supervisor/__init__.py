"""obscura.core.supervisor — Deterministic single-writer supervisor architecture.

Provides serialized session runs, frozen snapshots, event-sourced storage,
memory commit gating, and first-class hooks and heartbeats.

Quick start::

    from obscura.core.supervisor import Supervisor, SupervisorConfig

    supervisor = Supervisor(
        db_path="~/.obscura/supervisor.db",
        config=SupervisorConfig(
            lock_timeout=30.0,
            heartbeat_interval=5.0,
        ),
    )

    async for event in supervisor.run(
        session_id="sess-1",
        prompt="Fix the auth bug",
        backend=backend,
        tool_registry=tool_registry,
    ):
        print(event.kind, event.payload)

Components::

    Supervisor          — Main coordinator (single-writer per session)
    SessionStateMachine — Explicit state machine with invariant checking
    SessionLock         — SQLite advisory locks (cross-process safe)
    SessionHeartbeatManager — First-class heartbeat (persisted events)
    SessionHookManager  — Session-scoped hooks (persisted, replayable)
    FrozenToolRegistry  — Immutable tool snapshot per run
    PromptAssembler     — Deterministic prompt assembly with hashing
    MemoryCommitGate    — Memory commit gating with deduplication
    RunObserver         — Observability + drift detection
    AgentTemplateStore  — Agent templating + immutable versioning
    PolicyStore         — Immutable policy versioning
"""

from obscura.core.supervisor.agent_templates import (
    AgentTemplate,
    AgentTemplateStore,
    AgentVersion,
)
from obscura.core.supervisor.errors import (
    DriftDetectedError,
    ErrorCategory,
    LockAcquisitionError,
    LockExpiredError,
    MemoryCommitError,
    PromptAssemblyError,
    RunTimeoutError,
    StateTransitionError,
    SupervisorError,
    ToolExecutionError,
)
from obscura.core.supervisor.heartbeat import (
    SessionHeartbeatManager,
    get_heartbeats_for_run,
)
from obscura.core.supervisor.lock import SessionLock
from obscura.core.supervisor.memory_gate import (
    MemoryCommitGate,
    compute_memory_score,
    content_hash,
    recency_decay,
)
from obscura.core.supervisor.observability import RunMetrics, RunObserver
from obscura.core.supervisor.policy_store import PolicyStore, PolicyVersion
from obscura.core.supervisor.prompt_assembler import (
    SECTION_ORDER,
    PromptAssembler,
    format_tool_definitions,
)
from obscura.core.supervisor.schema import (
    REQUIRED_TABLES,
    init_supervisor_schema,
    verify_supervisor_schema,
)
from obscura.core.supervisor.session_hooks import SessionHookManager
from obscura.core.supervisor.state_machine import SessionStateMachine
from obscura.core.supervisor.supervisor import Supervisor
from obscura.core.supervisor.tool_snapshot import (
    FrozenToolEntry,
    FrozenToolRegistry,
    ToolSnapshotStore,
)
from obscura.core.supervisor.types import (
    VALID_SUPERVISOR_TRANSITIONS,
    LockInfo,
    MemoryCandidate,
    MemoryCommitResult,
    PromptSection,
    PromptSnapshot,
    RunContext,
    SessionHeartbeat,
    SupervisorConfig,
    SupervisorEvent,
    SupervisorEventKind,
    SupervisorHookPoint,
    SupervisorState,
)

__all__ = [
    "REQUIRED_TABLES",
    "SECTION_ORDER",
    "VALID_SUPERVISOR_TRANSITIONS",
    # Agent templates
    "AgentTemplate",
    "AgentTemplateStore",
    "AgentVersion",
    "DriftDetectedError",
    "ErrorCategory",
    # Tools
    "FrozenToolEntry",
    "FrozenToolRegistry",
    "LockAcquisitionError",
    "LockExpiredError",
    "LockInfo",
    "MemoryCandidate",
    "MemoryCommitError",
    # Memory
    "MemoryCommitGate",
    "MemoryCommitResult",
    # Policy
    "PolicyStore",
    "PolicyVersion",
    # Prompt
    "PromptAssembler",
    "PromptAssemblyError",
    "PromptSection",
    "PromptSnapshot",
    # Context
    "RunContext",
    "RunMetrics",
    # Observability
    "RunObserver",
    "RunTimeoutError",
    "SessionHeartbeat",
    # Heartbeat
    "SessionHeartbeatManager",
    # Hooks
    "SessionHookManager",
    # Lock
    "SessionLock",
    # State machine
    "SessionStateMachine",
    "StateTransitionError",
    # Main coordinator
    "Supervisor",
    "SupervisorConfig",
    # Errors
    "SupervisorError",
    # Events
    "SupervisorEvent",
    "SupervisorEventKind",
    "SupervisorHookPoint",
    "SupervisorState",
    "ToolExecutionError",
    "ToolSnapshotStore",
    "compute_memory_score",
    "content_hash",
    "format_tool_definitions",
    "get_heartbeats_for_run",
    # Schema
    "init_supervisor_schema",
    "recency_decay",
    "verify_supervisor_schema",
]
