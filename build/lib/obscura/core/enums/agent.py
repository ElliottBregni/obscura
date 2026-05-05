"""Agent-runtime enums: backends, roles, phases, events, peers.

Every member of these enums is a `StrEnum` whose value matches the wire
string used today on disk, on the wire, and in `events.db`. Promoting an
existing `Enum` to `StrEnum` does not change `.value` text — it only adds
implicit string equality, so legacy callers that compare against bare
strings keep working without churn.
"""

from __future__ import annotations

from enum import StrEnum


class Backend(StrEnum):
    """Supported LLM backends."""

    COPILOT = "copilot"

    CLAUDE = "claude"

    LOCALLLM = "localllm"

    OPENAI = "openai"

    CODEX = "codex"

    MOONSHOT = "moonshot"


class ExecutionMode(StrEnum):
    """How a request should be executed.

    The first two members preserve Round 1's contract (``UNIFIED`` /
    ``NATIVE``).  The remaining members come from the plan's §3.2 promotion
    of the four scattered Literal aliases used in
    ``schemas/templates.py``, ``agent/peers.py``, and ``routes/agents.py``;
    they live on the same enum so ``AgentConfig.mode`` carries one type
    across every callsite.
    """

    UNIFIED = "unified"
    NATIVE = "native"
    RUN = "run"
    LOOP = "loop"
    STREAM = "stream"
    STREAM_LOOP = "stream_loop"
    BLOCKING = "blocking"
    APER = "aper"


class Role(StrEnum):
    """Normalized message roles."""

    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL_RESULT = "tool_result"


class ChunkKind(StrEnum):
    """Normalized streaming event kinds.

    All backends must emit the full lifecycle:
    MESSAGE_START -> TEXT_DELTA / THINKING_DELTA / TOOL_USE_START ->
    TOOL_USE_DELTA -> TOOL_USE_END -> TOOL_RESULT -> DONE (with metadata).
    """

    MESSAGE_START = "message_start"

    TEXT_DELTA = "text_delta"

    THINKING_DELTA = "thinking_delta"

    TOOL_USE_START = "tool_use_start"

    TOOL_USE_DELTA = "tool_use_delta"

    TOOL_USE_END = "tool_use_end"

    TOOL_RESULT = "tool_result"

    DONE = "done"

    ERROR = "error"

    RATE_LIMIT = "rate_limit"

    TASK_STARTED = "task_started"

    TASK_PROGRESS = "task_progress"

    TASK_NOTIFICATION = "task_notification"

    MIRROR_ERROR = "mirror_error"


class HookPoint(StrEnum):
    """Lifecycle hook points common to both backends."""

    PRE_TOOL_USE = "pre_tool_use"

    POST_TOOL_USE = "post_tool_use"

    USER_PROMPT_SUBMITTED = "user_prompt_submitted"

    STOP = "stop"

    PRE_ANALYZE = "pre_analyze"

    POST_ANALYZE = "post_analyze"

    PRE_PLAN = "pre_plan"

    POST_PLAN = "post_plan"

    PRE_EXECUTE = "pre_execute"

    POST_EXECUTE = "post_execute"

    PRE_RESPOND = "pre_respond"

    POST_RESPOND = "post_respond"


class AgentPhase(StrEnum):
    """Phases in the Analyze -> Plan -> Execute -> Respond agent loop."""

    ANALYZE = "analyze"
    PLAN = "plan"
    EXECUTE = "execute"
    RESPOND = "respond"


class AgentEventKind(StrEnum):
    """Events yielded by the agent loop."""

    TEXT_DELTA = "text_delta"

    THINKING_DELTA = "thinking_delta"

    TOOL_CALL = "tool_call"

    TOOL_RESULT = "tool_result"

    CONFIRMATION_REQUEST = "confirmation_request"

    TURN_COMPLETE = "turn_complete"

    TURN_START = "turn_start"

    AGENT_DONE = "agent_done"

    STOP_CHECK = "stop_check"

    ERROR = "error"

    SESSION_PAUSED = "session_paused"

    USER_INPUT = "user_input"

    CONTEXT_COMPACT = "context_compact"

    AGENT_START = "agent_start"

    AGENT_STOP = "agent_stop"

    PREFLIGHT_PASS = "preflight_pass"

    PREFLIGHT_FAIL = "preflight_fail"

    TOOL_CALL_FAILURE = "tool_call_failure"

    SUBAGENT_START = "subagent_start"

    TASK_COMPLETED = "task_completed"

    PLAN_APPROVAL_REQUEST = "plan_approval_request"

    CORRECTION_INJECTED = "correction_injected"

    TASK_STARTED = "task_started"

    TASK_PROGRESS = "task_progress"

    TASK_NOTIFICATION = "task_notification"

    RATE_LIMIT_WARNING = "rate_limit_warning"

    MIRROR_ERROR = "mirror_error"


class APERMode(StrEnum):
    """Controls when the full APER cycle is used.

    ``ALWAYS``   -- 4 phases every time (current behaviour).
    ``AUTO``     -- complexity heuristic decides per-input.
    ``DISABLED`` -- skip APER, run execute phase only (simple loop).
    """

    ALWAYS = "always"
    AUTO = "auto"
    DISABLED = "disabled"


class AgentStatus(StrEnum):
    """Agent lifecycle states.

    Persisted by name (``state.status.name``); ``StrEnum`` values mirror
    the lower-cased name so `==` comparisons against today's wire text
    continue to behave identically. Callers that index `AgentStatus[name]`
    keep using the name string.
    """

    PENDING = "pending"

    RUNNING = "running"

    WAITING = "waiting"

    COMPLETED = "completed"

    FAILED = "failed"

    STOPPED = "stopped"


class PeerKind(StrEnum):
    """Discriminator for the three peer reference types."""

    LOCAL = "local"
    A2A_REMOTE = "a2a_remote"
    UNIX_SOCKET = "unix_socket"


class InvocationMode(StrEnum):
    """Semantics of a peer-to-peer invocation."""

    BLOCKING = "blocking"
    STREAMING = "streaming"
    LOOP = "loop"
    STREAM_LOOP = "stream_loop"
