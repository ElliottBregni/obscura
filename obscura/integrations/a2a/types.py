"""obscura.a2a.types — A2A protocol re-exports.

Wire-format-bound models live in :mod:`obscura.core.models.a2a` (the
canonical location used at every wire boundary). This module re-exports
them under the historical names that internal callers already use
(``Task``, ``TaskStatus``, ``StreamEvent``, ``Part``, ...) so existing
imports keep resolving. New code should import from
:mod:`obscura.core.models.a2a` directly.

A2A protocol method names live in
:class:`obscura.core.enums.protocol.A2AMethod`; task lifecycle states in
:class:`obscura.core.enums.protocol.A2ATaskState`; part-kind discriminator
values in :class:`obscura.core.enums.protocol.A2APartKind`. The agent card,
auth, and per-request configuration models stay defined here — they're
plain in-memory schemas, not protocol-bound.

Conforms to the A2A Protocol Specification v0.3.
"""

from __future__ import annotations

from typing import Any, TypeAlias

from pydantic import BaseModel, Field

from obscura.core.enums.protocol import (
    A2AMethod as A2AMethod,
    A2ARole as A2ARole,
    A2ATaskState as A2ATaskState,
)
from obscura.core.models.a2a import (
    A2AArtifactUpdateEvent,
    A2AMessage as A2AMessage,
    A2APart,
    A2APartAdapter as A2APartAdapter,
    A2AStatusUpdateEvent,
    A2ATask,
    A2ATaskMessage as A2ATaskMessage,
    A2ATaskMessageAdapter as A2ATaskMessageAdapter,
    A2ATaskStatus,
    Artifact as Artifact,
    DataPart as DataPart,
    FileContent as FileContent,
    FilePart as FilePart,
    TextPart as TextPart,
)

# ---------------------------------------------------------------------------
# Back-compat aliases for legacy import names
# ---------------------------------------------------------------------------

# Pre-rename names kept in scope; consumers that haven't migrated still resolve.
TaskState: TypeAlias = A2ATaskState  # noqa: UP040
TaskStatus = A2ATaskStatus
Task = A2ATask
TaskStatusUpdateEvent = A2AStatusUpdateEvent
TaskArtifactUpdateEvent = A2AArtifactUpdateEvent

# Part is the discriminated-union TYPE; consumers do isinstance(p, TextPart).
Part: TypeAlias = A2APart  # noqa: UP040

# StreamEvent is the union of streaming-event types — used as a return type.
StreamEvent: TypeAlias = A2AStatusUpdateEvent | A2AArtifactUpdateEvent  # noqa: UP040


# ---------------------------------------------------------------------------
# Task state machine
# ---------------------------------------------------------------------------


# Valid state transitions enforced by the task store.
VALID_TRANSITIONS: dict[TaskState, frozenset[TaskState]] = {
    TaskState.PENDING: frozenset(
        {TaskState.WORKING, TaskState.REJECTED, TaskState.CANCELED},
    ),
    TaskState.WORKING: frozenset(
        {
            TaskState.INPUT_REQUIRED,
            TaskState.AUTH_REQUIRED,
            TaskState.COMPLETED,
            TaskState.FAILED,
            TaskState.CANCELED,
        },
    ),
    TaskState.INPUT_REQUIRED: frozenset(
        {
            TaskState.WORKING,
            TaskState.CANCELED,
            TaskState.FAILED,
        },
    ),
    TaskState.AUTH_REQUIRED: frozenset(
        {
            TaskState.WORKING,
            TaskState.CANCELED,
            TaskState.FAILED,
        },
    ),
    TaskState.COMPLETED: frozenset(),
    TaskState.FAILED: frozenset(),
    TaskState.CANCELED: frozenset(),
    TaskState.REJECTED: frozenset(),
}

TERMINAL_STATES: frozenset[TaskState] = frozenset(
    {
        TaskState.COMPLETED,
        TaskState.FAILED,
        TaskState.CANCELED,
        TaskState.REJECTED,
    },
)


# ---------------------------------------------------------------------------
# Agent Card — in-memory schema, not wire-bound
# ---------------------------------------------------------------------------


class AgentSkill(BaseModel):
    """A capability offered by the agent."""

    id: str
    name: str
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    examples: list[str] = Field(default_factory=list)


class AuthScheme(BaseModel):
    """Authentication scheme declared by an agent."""

    type: str  # "apiKey", "http", "oauth2", "openIdConnect", "mutualTLS"
    scheme: str | None = None  # e.g. "bearer", "basic"
    in_: str | None = Field(default=None, alias="in")  # "header", "query", "cookie"
    name: str | None = None  # header/query param name
    flows: dict[str, Any] | None = None  # OAuth2 flows

    model_config = {"populate_by_name": True}


class AgentCapabilities(BaseModel):
    """Server-declared capabilities."""

    streaming: bool = True
    pushNotifications: bool = False
    extendedAgentCard: bool = False


class AgentCard(BaseModel):
    """A2A Agent Card — published at /.well-known/agent.json."""

    name: str
    description: str = ""
    url: str
    version: str = "1.0"
    protocolVersion: str = "0.3"
    skills: list[AgentSkill] = Field(default_factory=list[AgentSkill])
    defaultInputModes: list[str] = Field(default_factory=lambda: ["text/plain"])
    defaultOutputModes: list[str] = Field(default_factory=lambda: ["text/plain"])
    capabilities: AgentCapabilities = Field(default_factory=AgentCapabilities)
    securitySchemes: dict[str, AuthScheme] = Field(
        default_factory=dict[str, AuthScheme],
    )
    security: list[dict[str, list[str]]] = Field(
        default_factory=list[dict[str, list[str]]],
    )
    provider: dict[str, str] | None = None
    extensions: list[dict[str, Any]] | None = None


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class PushNotificationConfig(BaseModel):
    """Webhook config for async task updates."""

    url: str
    token: str | None = None
    authentication: dict[str, Any] | None = None


class SendMessageConfiguration(BaseModel):
    """Per-request configuration for message/send."""

    acceptedOutputModes: list[str] | None = None
    pushNotificationConfig: PushNotificationConfig | None = None
    historyLength: int | None = None
    blocking: bool = False


# ---------------------------------------------------------------------------
# Error types
# ---------------------------------------------------------------------------


class A2AError(Exception):
    """Base A2A protocol error."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


class TaskNotFoundError(A2AError):
    def __init__(self, task_id: str) -> None:
        super().__init__(-32001, f"Task not found: {task_id}", {"taskId": task_id})


class TaskNotCancelableError(A2AError):
    def __init__(self, task_id: str, state: str) -> None:
        super().__init__(
            -32002,
            f"Task {task_id} in state '{state}' cannot be canceled",
            {"taskId": task_id, "state": state},
        )


class InvalidTransitionError(A2AError):
    def __init__(self, task_id: str, from_state: str, to_state: str) -> None:
        super().__init__(
            -32003,
            f"Invalid transition: {from_state} → {to_state}",
            {"taskId": task_id, "fromState": from_state, "toState": to_state},
        )


class UnsupportedOperationError(A2AError):
    def __init__(self, operation: str) -> None:
        super().__init__(-32004, f"Unsupported operation: {operation}")


class VersionNotSupportedError(A2AError):
    def __init__(self, version: str) -> None:
        super().__init__(-32005, f"Version not supported: {version}")


__all__ = [
    "TERMINAL_STATES",
    "VALID_TRANSITIONS",
    "A2AError",
    "A2AMessage",
    "A2AMethod",
    "A2APart",
    "A2APartAdapter",
    "A2ARole",
    "A2ATask",
    "A2ATaskMessage",
    "A2ATaskMessageAdapter",
    "A2ATaskState",
    "A2ATaskStatus",
    "AgentCapabilities",
    "AgentCard",
    "AgentSkill",
    "Artifact",
    "AuthScheme",
    "DataPart",
    "FileContent",
    "FilePart",
    "InvalidTransitionError",
    "Part",
    "PushNotificationConfig",
    "SendMessageConfiguration",
    "StreamEvent",
    "Task",
    "TaskArtifactUpdateEvent",
    "TaskNotCancelableError",
    "TaskNotFoundError",
    "TaskState",
    "TaskStatus",
    "TaskStatusUpdateEvent",
    "TextPart",
    "UnsupportedOperationError",
    "VersionNotSupportedError",
]
