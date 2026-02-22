"""Tests for sdk.a2a.event_mapper — AgentEvent → A2A event mapping."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

from obscura.integrations.a2a.event_mapper import EventMapper
from obscura.integrations.a2a.types import (
    StreamEvent,
    TaskArtifactUpdateEvent,
    TaskState,
    TaskStatusUpdateEvent,
)
from obscura.core.types import AgentEvent, AgentEventKind


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _event(kind: AgentEventKind, **kwargs: object) -> AgentEvent:
    return AgentEvent(kind=kind, **kwargs)  # type: ignore[arg-type]


def _mapper() -> EventMapper:
    return EventMapper(task_id="task-001", context_id="ctx-001")


# ---------------------------------------------------------------------------
# Individual event kinds
# ---------------------------------------------------------------------------


class TestTurnStart:
    def test_produces_working_status(self) -> None:
        m = _mapper()
        results = m.map(_event(AgentEventKind.TURN_START))
        assert len(results) == 1
        assert isinstance(results[0], TaskStatusUpdateEvent)
        assert results[0].status.state == TaskState.WORKING
        assert not results[0].final


class TestTextDelta:
    def test_produces_artifact_update(self) -> None:
        m = _mapper()
        results = m.map(_event(AgentEventKind.TEXT_DELTA, text="Hello"))
        assert len(results) == 1
        assert isinstance(results[0], TaskArtifactUpdateEvent)
        assert results[0].append is True
        assert results[0].artifact.parts[0].text == "Hello"  # type: ignore[union-attr]

    def test_empty_text_produces_nothing(self) -> None:
        m = _mapper()
        results = m.map(_event(AgentEventKind.TEXT_DELTA, text=""))
        assert len(results) == 0

    def test_multiple_deltas_same_artifact(self) -> None:
        m = _mapper()
        r1 = m.map(_event(AgentEventKind.TEXT_DELTA, text="Hello "))
        r2 = m.map(_event(AgentEventKind.TEXT_DELTA, text="world"))
        assert r1[0].artifact.artifactId == r2[0].artifact.artifactId  # type: ignore[union-attr]


class TestThinkingDelta:
    def test_produces_nothing(self) -> None:
        m = _mapper()
        results = m.map(_event(AgentEventKind.THINKING_DELTA, text="hmm"))
        assert len(results) == 0


class TestToolCall:
    def test_produces_working_status(self) -> None:
        m = _mapper()
        results = m.map(_event(AgentEventKind.TOOL_CALL, tool_name="search"))
        assert len(results) == 1
        assert isinstance(results[0], TaskStatusUpdateEvent)
        assert results[0].status.state == TaskState.WORKING


class TestToolResult:
    def test_produces_nothing(self) -> None:
        m = _mapper()
        results = m.map(_event(AgentEventKind.TOOL_RESULT, tool_result="found it"))
        assert len(results) == 0


class TestConfirmationRequest:
    def test_produces_input_required(self) -> None:
        m = _mapper()
        results = m.map(_event(AgentEventKind.CONFIRMATION_REQUEST))
        assert len(results) == 1
        assert isinstance(results[0], TaskStatusUpdateEvent)
        assert results[0].status.state == TaskState.INPUT_REQUIRED


class TestTurnComplete:
    def test_no_artifact_produces_nothing(self) -> None:
        m = _mapper()
        results = m.map(_event(AgentEventKind.TURN_COMPLETE))
        assert len(results) == 0

    def test_closes_open_artifact(self) -> None:
        m = _mapper()
        m.map(_event(AgentEventKind.TEXT_DELTA, text="data"))
        results = m.map(_event(AgentEventKind.TURN_COMPLETE))
        assert len(results) == 1
        assert isinstance(results[0], TaskArtifactUpdateEvent)
        assert results[0].lastChunk is True

    def test_resets_artifact_id(self) -> None:
        m = _mapper()
        m.map(_event(AgentEventKind.TEXT_DELTA, text="data"))
        m.map(_event(AgentEventKind.TURN_COMPLETE))
        # Next text delta should start a new artifact
        r = m.map(_event(AgentEventKind.TEXT_DELTA, text="new"))
        assert isinstance(r[0], TaskArtifactUpdateEvent)
        assert r[0].artifact.artifactId != ""


class TestAgentDone:
    def test_produces_completed_final(self) -> None:
        m = _mapper()
        results = m.map(_event(AgentEventKind.AGENT_DONE))
        assert len(results) == 1
        assert isinstance(results[0], TaskStatusUpdateEvent)
        assert results[0].status.state == TaskState.COMPLETED
        assert results[0].final

    def test_closes_artifact_then_completes(self) -> None:
        m = _mapper()
        m.map(_event(AgentEventKind.TEXT_DELTA, text="output"))
        results = m.map(_event(AgentEventKind.AGENT_DONE))
        assert len(results) == 2
        assert isinstance(results[0], TaskArtifactUpdateEvent)
        assert results[0].lastChunk
        assert isinstance(results[1], TaskStatusUpdateEvent)
        assert results[1].final


class TestError:
    def test_produces_failed_final(self) -> None:
        m = _mapper()
        results = m.map(_event(AgentEventKind.ERROR, text="boom"))
        assert len(results) == 1
        assert isinstance(results[0], TaskStatusUpdateEvent)
        assert results[0].status.state == TaskState.FAILED
        assert results[0].final

    def test_closes_artifact_on_error(self) -> None:
        m = _mapper()
        m.map(_event(AgentEventKind.TEXT_DELTA, text="partial"))
        results = m.map(_event(AgentEventKind.ERROR, text="crash"))
        assert len(results) == 2
        assert isinstance(results[0], TaskArtifactUpdateEvent)
        assert results[0].lastChunk
        assert isinstance(results[1], TaskStatusUpdateEvent)
        assert results[1].status.state == TaskState.FAILED


# ---------------------------------------------------------------------------
# Lifecycle sequences
# ---------------------------------------------------------------------------


class TestFullSequence:
    def test_typical_stream(self) -> None:
        """TURN_START → TEXT_DELTA*3 → TURN_COMPLETE → AGENT_DONE."""
        m = _mapper()
        all_events: list[StreamEvent] = []

        all_events.extend(m.map(_event(AgentEventKind.TURN_START)))
        all_events.extend(m.map(_event(AgentEventKind.TEXT_DELTA, text="Hi ")))
        all_events.extend(m.map(_event(AgentEventKind.TEXT_DELTA, text="there ")))
        all_events.extend(m.map(_event(AgentEventKind.TEXT_DELTA, text="!")))
        all_events.extend(m.map(_event(AgentEventKind.TURN_COMPLETE)))
        all_events.extend(m.map(_event(AgentEventKind.AGENT_DONE)))

        # WORKING, 3x artifact, close-artifact, COMPLETED
        status_events = [e for e in all_events if isinstance(e, TaskStatusUpdateEvent)]
        artifact_events = [e for e in all_events if isinstance(e, TaskArtifactUpdateEvent)]

        assert len(status_events) == 2  # WORKING + COMPLETED
        assert status_events[0].status.state == TaskState.WORKING
        assert status_events[1].status.state == TaskState.COMPLETED
        assert status_events[1].final

        assert len(artifact_events) == 4  # 3 deltas + 1 close
        assert artifact_events[-1].lastChunk

    def test_confirmation_flow(self) -> None:
        """TURN_START → TOOL_CALL → CONFIRMATION_REQUEST (paused)."""
        m = _mapper()
        all_events: list[StreamEvent] = []

        all_events.extend(m.map(_event(AgentEventKind.TURN_START)))
        all_events.extend(m.map(_event(AgentEventKind.TOOL_CALL, tool_name="deploy")))
        all_events.extend(m.map(_event(AgentEventKind.CONFIRMATION_REQUEST)))

        states = [e.status.state for e in all_events if isinstance(e, TaskStatusUpdateEvent)]
        assert states == [TaskState.WORKING, TaskState.WORKING, TaskState.INPUT_REQUIRED]


class TestReset:
    def test_reset_clears_artifact_state(self) -> None:
        m = _mapper()
        m.map(_event(AgentEventKind.TEXT_DELTA, text="chunk"))
        assert m._artifact_id is not None
        m.reset()
        assert m._artifact_id is None
        assert m._chunk_count == 0


class TestEventMetadata:
    def test_task_id_on_all_events(self) -> None:
        m = _mapper()
        for kind in AgentEventKind:
            results = m.map(_event(kind, text="x", tool_name="t", tool_result="r"))
            for e in results:
                assert e.taskId == "task-001"
                assert e.contextId == "ctx-001"
            m.reset()
