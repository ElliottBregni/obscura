"""
sdk.a2a.types — A2A protocol data model.

Pydantic models for the full A2A specification: Tasks, Messages, Parts,
Artifacts, Agent Cards, streaming events, and JSON-RPC method routing.

Conforms to the A2A Protocol Specification v0.3.
"""

from __future__ import annotations

import enum
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Task state machine
# ---------------------------------------------------------------------------


class TaskState(str, enum.Enum):
    """A2A task lifecycle states."""

    PENDING = "pending"
    WORKING = "working"
    INPUT_REQUIRED = "input-required"
    AUTH_REQUIRED = "auth-required"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"
    REJECTED = "rejected"


# Valid state transitions enforced by the task store.
VALID_TRANSITIONS: dict[TaskState, frozenset[TaskState]] = {
    TaskState.PENDING: frozenset({TaskState.WORKING, TaskState.REJECTED, TaskState.CANCELED}),
    TaskState.WORKING: frozenset({
        TaskState.INPUT_REQUIRED,
        TaskState.AUTH_REQUIRED,
        TaskState.COMPLETED,
        TaskState.FAILED,
        TaskState.CANCELED,
    }),
    TaskState.INPUT_REQUIRED: frozenset({
        TaskState.WORKING,
        TaskState.CANCELED,
        TaskState.FAILED,
    }),
    TaskState.AUTH_REQUIRED: frozenset({
        TaskState.WORKING,
        TaskState.CANCELED,
        TaskState.FAILED,
    }),
    TaskState.COMPLETED: frozenset(),
    TaskState.FAILED: frozenset(),
    TaskState.CANCELED: frozenset(),
    TaskState.REJECTED: frozenset(),
}

TERMINAL_STATES: frozenset[TaskState] = frozenset({
    TaskState.COMPLETED,
    TaskState.FAILED,
    TaskState.CANCELED,
    TaskState.REJECTED,
})


# ---------------------------------------------------------------------------
# Parts — content containers
# ---------------------------------------------------------------------------


class TextPart(BaseModel):
    """Plain text content."""

    kind: Literal["text"] = "text"
    text: str
    metadata: dict[str, Any] | None = None


class FileContent(BaseModel):
    """File reference or inline bytes."""

    name: str | None = None
    mimeType: str | None = None
    bytes: str | None = None  # base64-encoded
    uri: str | None = None


class FilePart(BaseModel):
    """File content (inline bytes or URI reference)."""

    kind: Literal["file"] = "file"
    file: FileContent
    metadata: dict[str, Any] | None = None


class DataPart(BaseModel):
    """Structured JSON data."""

    kind: Literal["data"] = "data"
    data: dict[str, Any]
    metadata: dict[str, Any] | None = None


# Union type for all part kinds.
Part = TextPart | FilePart | DataPart


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


class A2AMessage(BaseModel):
    """A single communication turn between client and agent."""

    role: Literal["user", "agent"]
    messageId: str
    parts: list[Part]
    taskId: str | None = None
    contextId: str | None = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, Any] | None = None
    referenceTaskIds: list[str] | None = None


# ---------------------------------------------------------------------------
# Artifacts
# ---------------------------------------------------------------------------


class Artifact(BaseModel):
    """Agent-generated output (document, image, structured data)."""

    artifactId: str
    name: str | None = None
    parts: list[Part]
    metadata: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Task
# ---------------------------------------------------------------------------


class TaskStatus(BaseModel):
    """Current task status with optional message."""

    state: TaskState
    message: A2AMessage | None = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))


class Task(BaseModel):
    """The primary unit of work in A2A."""

    id: str
    contextId: str
    status: TaskStatus
    artifacts: list[Artifact] = Field(default_factory=list)
    history: list[A2AMessage] = Field(default_factory=list)
    kind: Literal["task"] = "task"
    metadata: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Streaming events
# ---------------------------------------------------------------------------


class TaskStatusUpdateEvent(BaseModel):
    """SSE event: task status changed."""

    taskId: str
    contextId: str
    status: TaskStatus
    final: bool = False
    kind: Literal["status-update"] = "status-update"


class TaskArtifactUpdateEvent(BaseModel):
    """SSE event: artifact created or appended."""

    taskId: str
    contextId: str
    artifact: Artifact
    append: bool = False
    lastChunk: bool = False
    kind: Literal["artifact-update"] = "artifact-update"


# Union for stream responses.
StreamEvent = TaskStatusUpdateEvent | TaskArtifactUpdateEvent


# ---------------------------------------------------------------------------
# Agent Card
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
    skills: list[AgentSkill] = Field(default_factory=list)
    defaultInputModes: list[str] = Field(default_factory=lambda: ["text/plain"])
    defaultOutputModes: list[str] = Field(default_factory=lambda: ["text/plain"])
    capabilities: AgentCapabilities = Field(default_factory=AgentCapabilities)
    securitySchemes: dict[str, AuthScheme] = Field(default_factory=dict)
    security: list[dict[str, list[str]]] = Field(default_factory=list)
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
# JSON-RPC
# ---------------------------------------------------------------------------


class A2AMethod(str, enum.Enum):
    """A2A JSON-RPC method names."""

    MESSAGE_SEND = "message/send"
    MESSAGE_STREAM = "message/stream"
    TASKS_GET = "tasks/get"
    TASKS_LIST = "tasks/list"
    TASKS_CANCEL = "tasks/cancel"
    TASKS_SUBSCRIBE = "tasks/subscribe"
    PUSH_CONFIG_CREATE = "tasks/pushNotificationConfig/create"
    PUSH_CONFIG_GET = "tasks/pushNotificationConfig/get"
    PUSH_CONFIG_LIST = "tasks/pushNotificationConfig/list"
    PUSH_CONFIG_DELETE = "tasks/pushNotificationConfig/delete"
    AGENT_CARD = "agent/authenticatedExtendedCard"


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
