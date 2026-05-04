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
    cast,
    override,
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
from obscura.core.enums.tools import ContentBlockKind, SideEffects, ToolChoiceMode
from obscura.core.models.content import (
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
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
    "ContentBlockKind",
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
    "SideEffects",
    "StreamChunk",
    "StreamMetadata",
    "TextBlock",
    "ThinkingBlock",
    "ToolCallContext",
    "ToolCallEnvelope",
    "ToolCallInfo",
    "ToolChoice",
    "ToolChoiceMode",
    "ToolErrorType",
    "ToolExecutionError",
    "ToolResultBlock",
    "ToolResultEnvelope",
    "ToolRouterCapable",
    "ToolSpec",
    "ToolUseBlock",
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


def _build_content_block(
    *,
    kind: str = "text",
    text: str = "",
    tool_name: str = "",
    tool_input: Mapping[str, Any] | None = None,
    tool_use_id: str = "",
    is_error: bool = False,
    args: Mapping[str, Any] | None = None,
    content: Any = None,
) -> Any:
    """Construct the right ``ContentBlock`` variant for legacy keyword callers."""
    block_kind = kind.value if isinstance(kind, ContentBlockKind) else kind
    if block_kind == ContentBlockKind.TEXT.value:
        return TextBlock(text=text)
    if block_kind == ContentBlockKind.THINKING.value:
        return ThinkingBlock(text=text)
    if block_kind == ContentBlockKind.TOOL_USE.value:
        merged_args: Mapping[str, Any] = (
            args if args is not None else (tool_input or {})
        )
        return ToolUseBlock(
            tool_use_id=tool_use_id,
            tool_name=tool_name,
            args=merged_args,
        )
    if block_kind == ContentBlockKind.TOOL_RESULT.value:
        body = content if content is not None else text
        return ToolResultBlock(
            tool_use_id=tool_use_id,
            content=body,
            is_error=is_error,
        )
    raise ValueError(f"Unknown content block kind: {kind!r}")


class _ContentBlockMeta(type):
    """Metaclass enabling ``ContentBlock(...)`` factory + ``isinstance`` checks.

    Legacy callers use ``ContentBlock(kind="text", text="...")`` to build a
    block, and check ``isinstance(b, ContentBlock)``. The discriminated
    Pydantic union in ``obscura.core.models.content`` cannot satisfy both
    contracts directly. This metaclass dispatches calls to the right
    variant and treats every variant as a virtual subclass.
    """

    @override
    def __call__(cls, **kwargs: Any) -> Any:
        return _build_content_block(**kwargs)

    @override
    def __instancecheck__(cls, instance: Any) -> bool:
        return isinstance(
            instance, (TextBlock, ThinkingBlock, ToolUseBlock, ToolResultBlock)
        )


class ContentBlock(metaclass=_ContentBlockMeta):
    """Compatibility facade for the discriminated content-block union.

    Calling ``ContentBlock(kind=..., ...)`` returns one of ``TextBlock``,
    ``ThinkingBlock``, ``ToolUseBlock``, or ``ToolResultBlock``. New code
    should construct the variant directly and ``match`` / ``isinstance``
    on the variant types. Use ``ContentBlock.from_dict(payload)`` to
    rebuild a block from persisted JSON.

    The class declares the union's superset of attributes so static type
    checkers see ``b.kind`` / ``b.text`` / ``b.tool_input`` / ``b.args``
    on the legacy facade — runtime values come from the variant Pydantic
    model the metaclass produces.
    """

    # Static-only attribute declarations that mirror the union's fields.
    # The runtime class is empty; instances are always the Pydantic
    # variants the metaclass forwards to.
    if TYPE_CHECKING:
        kind: ContentBlockKind
        text: str
        tool_name: str
        tool_input: Mapping[str, Any]
        tool_use_id: str
        is_error: bool
        args: Mapping[str, Any]
        content: str | list[TextBlock]

    @staticmethod
    def from_dict(payload: Mapping[str, Any]) -> Any:
        """Reconstruct a block from a previously serialized dict payload."""
        return _build_content_block(**dict(payload))


@dataclass(frozen=True)
class Message:
    """Normalized message from either backend.

    The ``raw`` field holds the original SDK object for escape-hatch access.
    All messages are tagged with session_id, agent_name, and model for full traceability.
    """

    role: Role
    content: list[Any]
    session_id: str | None = None
    agent_name: str | None = None
    model: str | None = None
    raw: Any = None
    backend: Backend | None = None

    @property
    def text(self) -> str:
        """Convenience: concatenate all text blocks."""
        return "".join(b.text for b in self.content if isinstance(b, TextBlock))


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
    # Accepts ``str`` at the construction boundary so legacy callers
    # (parallel_plan, plugin loader) pass ``"none"`` / ``"read"`` /
    # ``"write"`` unchanged. ``__post_init__`` normalises every accepted
    # value to a real ``SideEffects`` member, so reads always see the
    # enum.
    side_effects: SideEffects | str = SideEffects.NONE
    auth_scope: tuple[str, ...] = field(default_factory=lambda: ())
    rate_limit_per_minute: int = 0
    cost_hint: float = 0.0
    timeout_seconds: float = 60.0
    retries: int = 0
    examples: tuple[dict[str, Any], ...] = field(default_factory=lambda: ())
    capability: str = ""  # capability group ID (e.g. "git.ops")

    def __post_init__(self) -> None:
        # Coerce loose string ``side_effects`` from legacy callers (and
        # plugin manifests) into the enum so downstream callsites never
        # see a bare ``str`` here. Unknown values (e.g. fine-grained
        # legacy strings like "writes:fs", "network:read") fall back to
        # SideEffects.MUTATING — conservative default that prevents
        # speculation and forces confirmation.
        raw = cast(Any, self.side_effects)
        if not isinstance(raw, SideEffects):
            try:
                coerced = SideEffects(raw)
            except ValueError:
                # Unknown side_effects string — treat as state-changing
                # (safest default; downstream can opt back in via the
                # known enum values).
                coerced = SideEffects.MUTATING
            object.__setattr__(self, "side_effects", coerced)

    def is_concurrency_safe(self) -> bool:
        """Tools without side effects can run concurrently."""
        return self.side_effects == SideEffects.NONE


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

    mode: ToolChoiceMode = ToolChoiceMode.AUTO
    function_name: str = ""

    def __post_init__(self) -> None:
        raw = cast(Any, self.mode)
        if not isinstance(raw, ToolChoiceMode):
            object.__setattr__(self, "mode", ToolChoiceMode(raw))

    @classmethod
    def auto(cls) -> ToolChoice:
        """Let the model decide whether to call tools."""
        return cls(mode=ToolChoiceMode.AUTO)

    @classmethod
    def none(cls) -> ToolChoice:
        """Disable tool calling for this request."""
        return cls(mode=ToolChoiceMode.NONE)

    @classmethod
    def required(cls, name: str = "") -> ToolChoice:
        """Force tool calling. Optionally force a specific tool by name."""
        if name:
            return cls(mode=ToolChoiceMode.FUNCTION, function_name=name)
        return cls(mode=ToolChoiceMode.REQUIRED)


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
