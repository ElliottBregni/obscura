"""obscura.internal.types — Shared data types for the unified SDK wrapper.

Provides the normalized message format, streaming chunk type, tool specification,
session references, hook definitions, and the BackendProtocol that each backend
must implement.

Agent enums (`Backend`, `Role`, `ChunkKind`, `AgentPhase`, `HookPoint`,
`AgentEventKind`, `ExecutionMode`) live in `obscura.core.enums.agent` and are
re-exported from here for one release cycle so existing imports keep working.
"""

from __future__ import annotations

import enum
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from typing import (
    TYPE_CHECKING,
    Any,
    Protocol,
    runtime_checkable,
)

from obscura.core.enums.agent import (
    AgentEventKind,
    AgentPhase,
    Backend,
    ChunkKind,
    ExecutionMode,
    HookPoint,
    Role,
)

if TYPE_CHECKING:
    from obscura.core.tools import ToolRegistry


__all__ = [
    "AgentContext",
    "AgentEvent",
    "AgentEventKind",
    "AgentHookConfig",
    "AgentPhase",
    "Backend",
    "BackendCapabilities",
    "BackendProtocol",
    "ChunkKind",
    "ConfirmationCapable",
    "ContentBlock",
    "EFFORT_THINKING_BUDGETS",
    "EffortLevel",
    "ExecutionMode",
    "HookContext",
    "HookPoint",
    "Message",
    "NativeHandle",
    "ProviderNativeRequest",
    "Role",
    "SessionRef",
    "StreamChunk",
    "StreamMetadata",
    "ToolCallContext",
    "ToolCallEnvelope",
    "ToolCallInfo",
    "ToolChoice",
    "ToolErrorType",
    "ToolExecutionError",
    "ToolResultEnvelope",
    "ToolRouterCapable",
    "ToolSpec",
    "UnifiedRequest",
]


def _empty_str_any_dict() -> dict[str, Any]:
    return {}


def _empty_any_list() -> list[Any]:
    return []


# ---------------------------------------------------------------------------
# Execution mode and provider-native requests
# ---------------------------------------------------------------------------


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
    metadata: dict[str, Any] = field(default_factory=_empty_str_any_dict)
    native: ProviderNativeRequest | None = None


# ---------------------------------------------------------------------------
# Message types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContentBlock:
    """A single block within a message.

    Covers text, thinking/reasoning, tool invocations, and tool results.
    """

    kind: str  # "text", "thinking", "tool_use", "tool_result"
    text: str = ""
    tool_name: str = ""
    tool_input: dict[str, Any] = field(default_factory=_empty_str_any_dict)
    tool_use_id: str = ""
    is_error: bool = False


@dataclass(frozen=True)
class Message:
    """Normalized message from either backend.

    The ``raw`` field holds the original SDK object for escape-hatch access.
    All messages are tagged with session_id, agent_name, and model for full traceability.
    """

    role: Role
    content: list[ContentBlock]
    session_id: str | None = None
    agent_name: str | None = None
    model: str | None = None
    raw: Any = None
    backend: Backend | None = None

    @property
    def text(self) -> str:
        """Convenience: concatenate all text blocks."""
        return "".join(b.text for b in self.content if b.kind == "text")


# ---------------------------------------------------------------------------
# Streaming types
# ---------------------------------------------------------------------------


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
    output_schema: dict[str, Any] = field(default_factory=_empty_str_any_dict)
    _pydantic_model: type | None = None
    required_tier: str = "public"
    side_effects: str = "none"
    auth_scope: tuple[str, ...] = field(default_factory=lambda: ())
    rate_limit_per_minute: int = 0
    cost_hint: float = 0.0
    timeout_seconds: float = 60.0
    retries: int = 0
    examples: tuple[dict[str, Any], ...] = field(default_factory=lambda: ())
    capability: str = ""  # capability group ID (e.g. "git.ops")

    def is_concurrency_safe(self) -> bool:
        """Tools without side effects can run concurrently."""
        return not self.side_effects or self.side_effects == "none"


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


@dataclass(frozen=True)
class HookContext:
    """Context passed to hook callbacks."""

    hook: HookPoint
    tool_name: str = ""
    tool_input: dict[str, Any] = field(default_factory=_empty_str_any_dict)
    tool_output: Any = None
    message: Message | None = None
    prompt: str = ""


# ---------------------------------------------------------------------------
# Agent types (APER loop)
# ---------------------------------------------------------------------------


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
    results: list[Any] = field(default_factory=_empty_any_list)
    response: Any = None
    metadata: dict[str, Any] = field(default_factory=_empty_str_any_dict)


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


@runtime_checkable
class ToolRouterCapable(Protocol):
    """Optional capability — implemented by backends that support per-call tool routing."""

    def set_tool_router(self, router: Any) -> None: ...


@runtime_checkable
class ConfirmationCapable(Protocol):
    """Optional capability — implemented by backends that gate tool calls via a confirmation hook."""

    def enable_confirmation(
        self, confirm_fn: Callable[[str, dict[str, Any]], bool]
    ) -> None: ...


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Effort levels — thinking budget allocation
# ---------------------------------------------------------------------------


class EffortLevel(enum.StrEnum):
    """Effort level controlling thinking budget and response verbosity."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    MAX = "max"


EFFORT_THINKING_BUDGETS: dict[EffortLevel, int] = {
    EffortLevel.LOW: 1024,
    EffortLevel.MEDIUM: 4096,
    EffortLevel.HIGH: 16384,
    EffortLevel.MAX: 65536,
}


# ---------------------------------------------------------------------------
# Agent loop event types
# ---------------------------------------------------------------------------


@dataclass
class ToolCallInfo:
    """Extracted tool call from a model response."""

    tool_use_id: str
    name: str
    input: dict[str, Any] = field(default_factory=_empty_str_any_dict)
    raw: Any = None
    classification: Any = None


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
    output_level: str = ""  # "" = use tool's x-default-level


@dataclass(frozen=True)
class ToolCallEnvelope:
    """Canonical tool call envelope used by execution adapters."""

    call_id: str
    agent_id: str
    tool: str
    args: dict[str, Any] = field(default_factory=_empty_str_any_dict)
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

    def __iter__(self):
        """Allow tuple-unpacking compatibility used in some tests.

        Unpacks as: (call_id, result_text, is_error)
        For error results, prefer the error.message so legacy tests see a useful
        human-readable message (e.g. "Capability token invalid or expired.").
        """
        is_error = self.status == "error"
        if not is_error:
            result_text = (
                self.result if isinstance(self.result, str) else str(self.result)
            )
        elif self.error is not None:
            result_text = self.error.message
        else:
            result_text = "Tool error"
        yield self.call_id
        yield result_text
        yield is_error


@dataclass
class AgentEvent:
    """A single event from the agent loop.

    Events stream in order: TURN_START → TEXT_DELTA / THINKING_DELTA /
    TOOL_CALL / TOOL_RESULT → TURN_COMPLETE → ... → AGENT_DONE.
    """

    kind: AgentEventKind
    text: str = ""
    tool_name: str = ""
    tool_input: dict[str, Any] = field(default_factory=_empty_str_any_dict)
    tool_result: str = ""
    tool_use_id: str = ""
    is_error: bool = False
    turn: int = 0
    raw: Any = None
    metadata: StreamMetadata | None = None
