"""
sdk._types — Shared data types for the unified SDK wrapper.

Provides the normalized message format, streaming chunk type, tool specification,
session references, hook definitions, and the BackendProtocol that each backend
must implement.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Protocol, runtime_checkable


# ---------------------------------------------------------------------------
# Backend enum
# ---------------------------------------------------------------------------

class Backend(enum.Enum):
    """Supported LLM backends."""
    COPILOT = "copilot"
    CLAUDE = "claude"


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
    kind: str          # "text", "thinking", "tool_use", "tool_result"
    text: str = ""
    tool_name: str = ""
    tool_input: dict[str, Any] = field(default_factory=dict)
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
    """Normalized streaming event kinds."""
    TEXT_DELTA = "text_delta"
    THINKING_DELTA = "thinking_delta"
    TOOL_USE_START = "tool_use_start"
    TOOL_USE_DELTA = "tool_use_delta"
    TOOL_RESULT = "tool_result"
    DONE = "done"
    ERROR = "error"


@dataclass(frozen=True)
class StreamChunk:
    """A single streaming event, normalized across backends."""
    kind: ChunkKind
    text: str = ""
    tool_name: str = ""
    tool_input_delta: str = ""
    raw: Any = None


# ---------------------------------------------------------------------------
# Tool specification
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ToolSpec:
    """A tool definition that works with both backends.

    Parameters should be a JSON Schema object. The optional _pydantic_model
    is used by the Copilot backend for native Pydantic integration.
    """
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., Any]
    _pydantic_model: type | None = None


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
# Hook types
# ---------------------------------------------------------------------------

class HookPoint(enum.Enum):
    """Lifecycle hook points common to both backends."""
    PRE_TOOL_USE = "pre_tool_use"
    POST_TOOL_USE = "post_tool_use"
    USER_PROMPT_SUBMITTED = "user_prompt_submitted"
    STOP = "stop"


@dataclass(frozen=True)
class HookContext:
    """Context passed to hook callbacks."""
    hook: HookPoint
    tool_name: str = ""
    tool_input: dict[str, Any] = field(default_factory=dict)
    tool_output: Any = None
    message: Message | None = None


# ---------------------------------------------------------------------------
# Backend protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class BackendProtocol(Protocol):
    """Contract that each backend implementation must satisfy."""

    async def start(self) -> None: ...
    async def stop(self) -> None: ...

    async def send(self, prompt: str, **kwargs: Any) -> Message: ...
    async def stream(self, prompt: str, **kwargs: Any) -> AsyncIterator[StreamChunk]: ...

    async def create_session(self, **kwargs: Any) -> SessionRef: ...
    async def resume_session(self, ref: SessionRef) -> None: ...
    async def list_sessions(self) -> list[SessionRef]: ...
    async def delete_session(self, ref: SessionRef) -> None: ...

    def register_tool(self, spec: ToolSpec) -> None: ...
    def register_hook(self, hook: HookPoint, callback: Callable) -> None: ...
