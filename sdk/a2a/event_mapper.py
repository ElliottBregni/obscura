"""
sdk.a2a.event_mapper — Bidirectional AgentEvent ↔ A2A event mapping.

Converts Obscura's ``AgentEvent`` stream into A2A protocol events
(``TaskStatusUpdateEvent``, ``TaskArtifactUpdateEvent``) and vice versa.

Critical mappings:
    CONFIRMATION_REQUEST → INPUT_REQUIRED (task pauses for user input)
    AGENT_DONE           → COMPLETED      (terminal)
    ERROR                → FAILED         (terminal)
    TEXT_DELTA            → ArtifactUpdate (append=True)
    TURN_START            → StatusUpdate   (WORKING)
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sdk.a2a.types import (
    Artifact,
    StreamEvent,
    TaskArtifactUpdateEvent,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
    TextPart,
)
from sdk.internal.types import AgentEvent, AgentEventKind


class EventMapper:
    """Converts AgentEvent objects to A2A streaming events.

    Maintains per-task artifact state so successive TEXT_DELTA events
    accumulate into a single artifact that is appended chunk-by-chunk.
    """

    def __init__(self, task_id: str, context_id: str) -> None:
        self._task_id = task_id
        self._context_id = context_id
        self._artifact_id: str | None = None
        self._chunk_count = 0

    def status_event(
        self, state: TaskState, *, final: bool = False, message: str | None = None,
    ) -> TaskStatusUpdateEvent:
        status = TaskStatus(
            state=state,
            timestamp=datetime.now(UTC),
        )
        return TaskStatusUpdateEvent(
            taskId=self._task_id,
            contextId=self._context_id,
            status=status,
            final=final,
        )

    def _artifact_event(
        self, text: str, *, append: bool = True, last_chunk: bool = False,
    ) -> TaskArtifactUpdateEvent:
        if self._artifact_id is None:
            self._artifact_id = f"art-{uuid.uuid4().hex[:8]}"
            self._chunk_count = 0

        self._chunk_count += 1

        return TaskArtifactUpdateEvent(
            taskId=self._task_id,
            contextId=self._context_id,
            artifact=Artifact(
                artifactId=self._artifact_id,
                parts=[TextPart(text=text)],
            ),
            append=append,
            lastChunk=last_chunk,
        )

    def map(self, event: AgentEvent) -> list[StreamEvent]:
        """Map a single AgentEvent to zero or more A2A stream events.

        Returns a list because some events produce multiple A2A events
        (e.g., AGENT_DONE produces a final artifact chunk + status update).
        """
        kind = event.kind

        if kind == AgentEventKind.TURN_START:
            return [self.status_event(TaskState.WORKING)]

        if kind == AgentEventKind.TEXT_DELTA:
            if event.text:
                return [self._artifact_event(event.text, append=True)]
            return []

        if kind == AgentEventKind.THINKING_DELTA:
            # Thinking is internal — not exposed to A2A clients.
            return []

        if kind == AgentEventKind.TOOL_CALL:
            # Inform client that a tool is being invoked.
            return [self.status_event(TaskState.WORKING)]

        if kind == AgentEventKind.TOOL_RESULT:
            # Tool results are internal; the agent will incorporate them
            # into its next text response.
            return []

        if kind == AgentEventKind.CONFIRMATION_REQUEST:
            return [self.status_event(TaskState.INPUT_REQUIRED)]

        if kind == AgentEventKind.TURN_COMPLETE:
            # Close the current artifact if one was being streamed.
            events: list[StreamEvent] = []
            if self._artifact_id is not None:
                events.append(
                    self._artifact_event("", append=True, last_chunk=True)
                )
                self._artifact_id = None
            return events

        if kind == AgentEventKind.AGENT_DONE:
            events = []
            # Close any open artifact.
            if self._artifact_id is not None:
                events.append(
                    self._artifact_event("", append=True, last_chunk=True)
                )
                self._artifact_id = None
            events.append(self.status_event(TaskState.COMPLETED, final=True))
            return events

        if kind == AgentEventKind.ERROR:
            events = []
            if self._artifact_id is not None:
                events.append(
                    self._artifact_event("", append=True, last_chunk=True)
                )
                self._artifact_id = None
            events.append(self.status_event(TaskState.FAILED, final=True))
            return events

        # Unknown event kind — ignore gracefully.
        return []

    def reset(self) -> None:
        """Reset mapper state (for reuse across turns)."""
        self._artifact_id = None
        self._chunk_count = 0
