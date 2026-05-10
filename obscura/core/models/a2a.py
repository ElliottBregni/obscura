"""A2A protocol boundary models — Part union, Message, Task, streaming events.

Implements the wire-format-bound subset of the A2A v0.3 specification using
discriminated unions on ``kind``. The :data:`A2APart` adapter is the Phase 3
deliverable that lives at the boundary: ``TextPart | FilePart | DataPart``
discriminated by :class:`A2APartKind`.

The discriminator pattern uses ``kind: Literal[A2APartKind.TEXT.value] =
A2APartKind.TEXT.value`` rather than the enum member directly. Pydantic v2's
``Field(discriminator=...)`` matches against the literal value, and using
``.value`` rather than the member object keeps the JSON output as
``"kind": "text"`` instead of ``"kind": A2APartKind.TEXT``.

``A2ATask`` and ``A2ATaskStatus`` use a non-frozen base because the in-memory
task store mutates them in place (``task.status = ...``). The serialised wire
format is still fully validated on egress through ``model_dump``. Boundary
parts and stream events stay frozen — they're constructed once and shipped.

Re-exported from :mod:`obscura.integrations.a2a.types` so existing imports
keep resolving.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from obscura.core.enums.protocol import (
    A2APartKind,
    A2ARole,
    A2ATaskMessageKind,
    A2ATaskState,
)
from obscura.core.models._base import BoundaryModel


class _MutableA2AModel(BaseModel):
    """Non-frozen variant of :class:`BoundaryModel`.

    Used for the few internal-state aggregate types (``A2ATask``,
    ``A2ATaskStatus``) that the in-memory store rebinds in place. Wire
    semantics are otherwise identical: lenient ``extra="ignore"`` for forward
    compat, populate-by-name so aliased camelCase keys round-trip.
    """

    model_config = ConfigDict(
        frozen=False,
        extra="ignore",
        strict=False,
        populate_by_name=True,
    )


# ---------------------------------------------------------------------------
# Parts — discriminated on `kind`
# ---------------------------------------------------------------------------


class TextPart(BoundaryModel):
    """Plain text content."""

    kind: Literal["text"] = A2APartKind.TEXT.value
    text: str
    metadata: Mapping[str, Any] | None = None


class FileContent(BoundaryModel):
    """File reference or inline bytes."""

    name: str | None = None
    mimeType: str | None = None
    bytes: str | None = None
    uri: str | None = None


class FilePart(BoundaryModel):
    """File content (inline bytes or URI reference)."""

    kind: Literal["file"] = A2APartKind.FILE.value
    file: FileContent
    metadata: Mapping[str, Any] | None = None


class DataPart(BoundaryModel):
    """Structured JSON data."""

    kind: Literal["data"] = A2APartKind.DATA.value
    data: Mapping[str, Any]
    metadata: Mapping[str, Any] | None = None


A2APart = Annotated[
    Union[TextPart, FilePart, DataPart],  # noqa: UP007
    Field(discriminator="kind"),
]
"""Discriminated union of A2A message parts.

Validate runtime payloads with ``TypeAdapter(A2APart).validate_python(raw)``.
"""

A2APartAdapter: TypeAdapter[TextPart | FilePart | DataPart] = TypeAdapter(A2APart)


# ---------------------------------------------------------------------------
# Messages and artifacts
# ---------------------------------------------------------------------------


class A2AMessage(BoundaryModel):
    """A single communication turn between client and agent."""

    role: A2ARole
    messageId: str
    parts: list[TextPart | FilePart | DataPart]
    taskId: str | None = None
    contextId: str | None = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    metadata: Mapping[str, Any] | None = None
    referenceTaskIds: list[str] | None = None

    @model_validator(mode="before")
    @classmethod
    def _auto_message_id(cls, data: Any) -> Any:
        """Auto-generate a UUID4 messageId if absent or empty."""
        if isinstance(data, dict) and not data.get("messageId"):
            data = dict(data)
            data["messageId"] = str(uuid.uuid4())
        return data


class Artifact(BoundaryModel):
    """Agent-generated output (document, image, structured data)."""

    artifactId: str
    name: str | None = None
    parts: list[TextPart | FilePart | DataPart]
    metadata: Mapping[str, Any] | None = None


# ---------------------------------------------------------------------------
# Task and streaming events — discriminated on `kind`
# ---------------------------------------------------------------------------


class A2ATaskStatus(_MutableA2AModel):
    """Current task status with optional message.

    Non-frozen because :class:`A2ATask` rebinds it during state transitions.
    """

    state: A2ATaskState
    message: A2AMessage | None = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))


class A2ATask(_MutableA2AModel):
    """The primary unit of work in A2A.

    Non-frozen because the in-memory store mutates ``status`` and appends to
    ``history`` / ``artifacts`` in place during the task lifecycle.
    """

    kind: Literal["task"] = A2ATaskMessageKind.TASK.value
    id: str
    contextId: str
    status: A2ATaskStatus
    artifacts: list[Artifact] = Field(default_factory=list[Artifact])
    history: list[A2AMessage] = Field(default_factory=list[A2AMessage])
    metadata: Mapping[str, Any] | None = None


class A2AStatusUpdateEvent(BoundaryModel):
    """SSE event: task status changed."""

    kind: Literal["status-update"] = A2ATaskMessageKind.STATUS_UPDATE.value
    taskId: str
    contextId: str
    status: A2ATaskStatus
    final: bool = False


class A2AArtifactUpdateEvent(BoundaryModel):
    """SSE event: artifact created or appended."""

    kind: Literal["artifact-update"] = A2ATaskMessageKind.ARTIFACT_UPDATE.value
    taskId: str
    contextId: str
    artifact: Artifact
    append: bool = False
    lastChunk: bool = False


A2ATaskMessage = Annotated[
    Union[A2ATask, A2AStatusUpdateEvent, A2AArtifactUpdateEvent],  # noqa: UP007
    Field(discriminator="kind"),
]
"""Discriminated union of A2A task envelopes and streaming events."""

A2ATaskMessageAdapter: TypeAdapter[
    A2ATask | A2AStatusUpdateEvent | A2AArtifactUpdateEvent
] = TypeAdapter(A2ATaskMessage)


__all__ = [
    "A2AArtifactUpdateEvent",
    "A2AMessage",
    "A2APart",
    "A2APartAdapter",
    "A2AStatusUpdateEvent",
    "A2ATask",
    "A2ATaskMessage",
    "A2ATaskMessageAdapter",
    "A2ATaskStatus",
    "Artifact",
    "DataPart",
    "FileContent",
    "FilePart",
    "TextPart",
]
