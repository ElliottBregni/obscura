"""
obscura.internal.types — Shared data types for the unified SDK wrapper.

Provides the normalized message format, streaming chunk type, tool specification,
session references, hook definitions, and the BackendProtocol that each backend
must implement.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import (
    TYPE_CHECKING,
    Any,
    AsyncIterator,
    Callable,
    Mapping,
    Protocol,
    runtime_checkable,
)

if TYPE_CHECKING:
    from obscura.core.tools import ToolRegistry


# ---------------------------------------------------------------------------
# Backend enum
# ---------------------------------------------------------------------------


class Backend(enum.Enum):
    """Supported LLM backends."""

    COPILOT = "copilot"
    CLAUDE = "claude"
    LOCALLLM = "localllm"
    OPENAI = "openai"
    CODEX = "codex"
    MOONSHOT = "moonshot"


# ---------------------------------------------------------------------------
# Execution mode and provider-native requests
# ---------------------------------------------------------------------------


class ExecutionMode(enum.Enum):
    """How a request should be executed.

    ``UNIFIED`` uses Obscura's normalized contract and event model.
    ``NATIVE`` preserves provider semantics and metadata as-is.
    """

    UNIFIED = "unified"
    NATIVE = "native"


@dataclass(frozen=True)
class ProviderNativeRequest:
    """Provider-specific request payloads for native mode.

    Each payload is passed through to the corresponding backend adapter.
    """

    openai: Mapping[str, Any] | None = None
    codex: Mapping[str, Any] | None = None
    moonshot: Mapping[str, Any] | None = None
    claude: Mapping[str, Any] | None = None
    copilot: Mapping[str, Any] | None = None
    localllm: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class UnifiedRequest:
    """Versioned request envelope for future unified/native routing."""

    prompt: str = ""
    mode: ExecutionMode = ExecutionMode.UNIFIED
    messages: list[Message] | None = None
    tool_choice: ToolChoice | None = None
    session: SessionRef | None = None
    metadata: dict[str, Any] = field(default_factory=lambda: {})
    native: ProviderNativeRequest | None = None


# ---------------------------------------------------------------------------
# Message types
# ---------------------------------------------------------------------------


class Role(enum.Enum):
    """Normalized message roles."""

    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL_RESULT = "tool_result"


@dataclass(frozen=True)
class ContentBlock:
    """A single block within a message.

    Covers text, thinking/reasoning, tool invocations, and tool results.
    """

    kind: str  # "text", "thinking", "tool_use", "tool_result"
    text: str = ""
    tool_name: str = ""
    tool_input: dict[str, Any] = field(default_factory=lambda: {})
    tool_use_id: str = ""
    is_error: bool = False


@dataclass(frozen=True)
class Message:
    """Normalized message from either backend.

    The ``raw`` field holds the original SDK object for escape-hatch access.
    """

    role: Role
    content: list[ContentBlock]
    raw: Any = None
    backend: Backend | None = None

    @property
    def text(self) -> str:
        """Convenience: concatenate all text blocks."""
        return "".join(b.text for b in self.content if b.kind == "text")


# ---------------------------------------------------------------------------
# Streaming types
# ---------------------------------------------------------------------------


class ChunkKind(enum.Enum):
    """Normalized streaming event kinds.

    All backends must emit the full lifecycle:
    MESSAGE_START → TEXT_DELTA / THINKING_DELTA / TOOL_USE_START →
    TOOL_USE_DELTA → TOOL_USE_END → TOOL_RESULT → DONE (with metadata).
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


@dataclass(frozen=True)
class StreamMetadata:
    """Metadata extracted from a completed stream.

    Attached to the ``DONE`` chunk so callers never lose provider metadata.
    """

    finish_reason: str = ""
    usage: dict[str, int] | None = None
    model_id: str = ""
    session_id: str = ""


@dataclass(frozen=True)
class StreamChunk:
    """A single streaming event, normalized across backends."""

    kind: ChunkKind
    text: str = ""
    tool_name: str = ""
    tool_input_delta: str = ""
    tool_use_id: str = ""
    raw: Any = None
    metadata: StreamMetadata | None = None
    native_event: Any = None


# ---------------------------------------------------------------------------
# Tool specification
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolSpec:
    """A tool definition that works with both backends.

    Parameters should be a JSON Schema object. The optional _pydantic_model
    is used by the Copilot backend for native Pydantic integration.

    The ``required_tier`` field declares the minimum capability tier
    needed to execute this tool (``"public"`` or ``"privileged"``).
    Defaults to ``"public"`` for backward compatibility.
    """

    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., Any]
    output_schema: dict[str, Any] = field(default_factory=lambda: {})
    _pydantic_model: type | None = None
    required_tier: str = "public"
    side_effects: str = "none"
    auth_scope: tuple[str, ...] = field(default_factory=lambda: ())
    rate_limit_per_minute: int = 0
    cost_hint: float = 0.0
    timeout_seconds: float = 60.0
    retries: int = 0
    examples: tuple[dict[str, Any], ...] = field(default_factory=lambda: ())


# Hook config type for Copilot backend
AgentHookConfig = dict[str, Callable[..., Any]]


# ---------------------------------------------------------------------------
# Session reference
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionRef:
    """An opaque reference to a backend session."""

    session_id: str
    backend: Backend
    raw: Any = None


# ---------------------------------------------------------------------------
# Tool policy
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolChoice:
    """Per-request tool selection policy.

    Use the factory classmethods for clean construction::

        ToolChoice.auto()
        ToolChoice.none()
        ToolChoice.required()
        ToolChoice.required("my_tool")
    """

    mode: str = "auto"
    function_name: str = ""

    @classmethod
    def auto(cls) -> ToolChoice:
        """Let the model decide whether to call tools."""
        return cls(mode="auto")

    @classmethod
    def none(cls) -> ToolChoice:
        """Disable tool calling for this request."""
        return cls(mode="none")

    @classmethod
    def required(cls, name: str = "") -> ToolChoice:
        """Force tool calling. Optionally force a specific tool by name."""
        if name:
            return cls(mode="function", function_name=name)
        return cls(mode="required")


# ---------------------------------------------------------------------------
# Backend capabilities
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BackendCapabilities:
    """Declares what a backend supports in unified mode.

    Agent runtime adapts behaviour based on these flags rather than
    silently ignoring unsupported features.
    """

    supports_streaming: bool = True
    supports_tool_calls: bool = False
    supports_tool_choice: bool = False
    supports_usage: bool = False
    supports_reasoning: bool = False
    supports_remote_sessions: bool = False
    supports_multimodal: bool = False
    supports_mcp: bool = False
    supports_native_mode: bool = True
    native_features: tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Native mode handle
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NativeHandle:
    """Stable accessor for raw provider SDK objects.

    Use ``backend.native`` instead of reaching into private attributes.
    """

    client: Any = None
    session: Any = None
    meta: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Hook types
# ---------------------------------------------------------------------------


class HookPoint(enum.Enum):
    """Lifecycle hook points common to both backends."""

    PRE_TOOL_USE = "pre_tool_use"
    POST_TOOL_USE = "post_tool_use"
    USER_PROMPT_SUBMITTED = "user_prompt_submitted"
    STOP = "stop"
    # Agent-loop hooks (APER) — symmetric PRE/POST for every phase
    PRE_ANALYZE = "pre_analyze"
    POST_ANALYZE = "post_analyze"
    PRE_PLAN = "pre_plan"
    POST_PLAN = "post_plan"
    PRE_EXECUTE = "pre_execute"
    POST_EXECUTE = "post_execute"
    PRE_RESPOND = "pre_respond"
    POST_RESPOND = "post_respond"


@dataclass(frozen=True)
class HookContext:
    """Context passed to hook callbacks."""

    hook: HookPoint
    tool_name: str = ""
    tool_input: dict[str, Any] = field(default_factory=lambda: {})
    tool_output: Any = None
    message: Message | None = None
    prompt: str = ""


# ---------------------------------------------------------------------------
# Agent types (APER loop)
# ---------------------------------------------------------------------------


class AgentPhase(enum.Enum):
    """Phases in the Analyze → Plan → Execute → Respond agent loop."""

    ANALYZE = "analyze"
    PLAN = "plan"
    EXECUTE = "execute"
    RESPOND = "respond"


@dataclass
class AgentContext:
    """Mutable context passed through the APER loop.

    Each phase reads from and writes to this context. The ``metadata``
    dict carries arbitrary data (system prompts, role context, etc.).
    """

    phase: AgentPhase
    input_data: Any = None
    analysis: Any = None
    plan: Any = None
    results: list[Any] = field(default_factory=lambda: [])
    response: Any = None
    metadata: dict[str, Any] = field(default_factory=lambda: {})


# ---------------------------------------------------------------------------
# Backend protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class BackendProtocol(Protocol):
    """Contract that each backend implementation must satisfy."""

    async def start(self) -> None: ...
    async def stop(self) -> None: ...

    async def send(self, prompt: str, **kwargs: Any) -> Message: ...
    def stream(self, prompt: str, **kwargs: Any) -> AsyncIterator[StreamChunk]: ...

    async def create_session(self, **kwargs: Any) -> SessionRef: ...
    async def resume_session(self, ref: SessionRef) -> None: ...
    async def list_sessions(self) -> list[SessionRef]: ...
    async def delete_session(self, ref: SessionRef) -> None: ...

    def register_tool(self, spec: ToolSpec) -> None: ...
    def register_hook(self, hook: HookPoint, callback: Callable[..., Any]) -> None: ...

    def get_tool_registry(self) -> ToolRegistry: ...

    @property
    def native(self) -> NativeHandle: ...
    def capabilities(self) -> BackendCapabilities: ...


# ---------------------------------------------------------------------------
# Agent loop event types
# ---------------------------------------------------------------------------


class AgentEventKind(enum.Enum):
    """Events yielded by the agent loop."""

    TEXT_DELTA = "text_delta"
    THINKING_DELTA = "thinking_delta"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    CONFIRMATION_REQUEST = "confirmation_request"
    TURN_COMPLETE = "turn_complete"
    TURN_START = "turn_start"
    AGENT_DONE = "agent_done"
    ERROR = "error"
    SESSION_PAUSED = "session_paused"
    USER_INPUT = "user_input"


@dataclass
class ToolCallInfo:
    """Extracted tool call from a model response."""

    tool_use_id: str
    name: str
    input: dict[str, Any] = field(default_factory=lambda: {})
    raw: Any = None


class ToolErrorType(enum.Enum):
    """Normalized error types for tool execution."""

    INVALID_ARGS = "INVALID_ARGS"
    UNAUTHORIZED = "UNAUTHORIZED"
    NOT_FOUND = "NOT_FOUND"
    RATE_LIMITED = "RATE_LIMITED"
    TIMEOUT = "TIMEOUT"
    DEPENDENCY_ERROR = "DEPENDENCY_ERROR"
    CONFLICT = "CONFLICT"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class ToolCallContext:
    """Execution context attached to each tool call."""

    trace_id: str = ""
    user_id: str = ""
    policy: str = ""


@dataclass(frozen=True)
class ToolCallEnvelope:
    """Canonical tool call envelope used by execution adapters."""

    call_id: str
    agent_id: str
    tool: str
    args: dict[str, Any] = field(default_factory=lambda: {})
    context: ToolCallContext = field(default_factory=ToolCallContext)


@dataclass(frozen=True)
class ToolExecutionError:
    """Normalized tool error payload."""

    type: ToolErrorType
    message: str
    retry_after_ms: int | None = None
    safe_to_retry: bool = False


@dataclass(frozen=True)
class ToolResultEnvelope:
    """Canonical tool result envelope."""

    call_id: str
    tool: str
    status: str
    result: Any = None
    error: ToolExecutionError | None = None
    latency_ms: int = 0
    cost: float = 0.0
    tool_use_id: str = ""
    raw: Any = None


@dataclass
class AgentEvent:
    """A single event from the agent loop.

    Events stream in order: TURN_START → TEXT_DELTA / THINKING_DELTA /
    TOOL_CALL / TOOL_RESULT → TURN_COMPLETE → ... → AGENT_DONE.
    """

    kind: AgentEventKind
    text: str = ""
    tool_name: str = ""
    tool_input: dict[str, Any] = field(default_factory=lambda: {})
    tool_result: str = ""
    tool_use_id: str = ""
    is_error: bool = False
    turn: int = 0
    raw: Any = None
