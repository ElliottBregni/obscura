"""Tests for obscura.core.event_store — Durable event-sourced session persistence."""

from __future__ import annotations

from pathlib import Path

import pytest

from obscura.core.event_store import (
    SQLiteEventStore,
    SessionStatus,
    VALID_TRANSITIONS,
)
from obscura.core.types import AgentEvent, AgentEventKind


@pytest.fixture
def store(tmp_path: Path) -> SQLiteEventStore:
    """Create a fresh SQLiteEventStore per test."""
    return SQLiteEventStore(tmp_path / "test_events.db")


# ---------------------------------------------------------------------------
# Session CRUD
# ---------------------------------------------------------------------------


class TestSessionLifecycle:
    @pytest.mark.asyncio
    async def test_create_session(self, store: SQLiteEventStore) -> None:
        rec = await store.create_session("sess-1", agent="oncall")
        assert rec.id == "sess-1"
        assert rec.status == SessionStatus.RUNNING
        assert rec.active_agent == "oncall"

    @pytest.mark.asyncio
    async def test_get_session(self, store: SQLiteEventStore) -> None:
        await store.create_session("sess-1", agent="oncall")
        rec = await store.get_session("sess-1")
        assert rec is not None
        assert rec.id == "sess-1"

    @pytest.mark.asyncio
    async def test_get_missing_session(self, store: SQLiteEventStore) -> None:
        rec = await store.get_session("nonexistent")
        assert rec is None

    @pytest.mark.asyncio
    async def test_list_sessions(self, store: SQLiteEventStore) -> None:
        await store.create_session("a", agent="agent-a")
        await store.create_session("b", agent="agent-b")
        sessions = await store.list_sessions()
        assert len(sessions) == 2

    @pytest.mark.asyncio
    async def test_list_sessions_filter_by_status(
        self, store: SQLiteEventStore
    ) -> None:
        await store.create_session("a", agent="x")
        await store.create_session("b", agent="y")
        await store.update_status("a", SessionStatus.COMPLETED)
        running = await store.list_sessions(status=SessionStatus.RUNNING)
        assert len(running) == 1
        assert running[0].id == "b"


# ---------------------------------------------------------------------------
# Status transitions
# ---------------------------------------------------------------------------


class TestStatusTransitions:
    @pytest.mark.asyncio
    async def test_valid_transition(self, store: SQLiteEventStore) -> None:
        await store.create_session("s", agent="a")
        await store.update_status("s", SessionStatus.COMPLETED)
        rec = await store.get_session("s")
        assert rec is not None
        assert rec.status == SessionStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_invalid_transition_raises(self, store: SQLiteEventStore) -> None:
        await store.create_session("s", agent="a")
        await store.update_status("s", SessionStatus.COMPLETED)
        with pytest.raises(ValueError, match="Invalid transition"):
            await store.update_status("s", SessionStatus.RUNNING)

    @pytest.mark.asyncio
    async def test_missing_session_raises(self, store: SQLiteEventStore) -> None:
        with pytest.raises(ValueError, match="Session not found"):
            await store.update_status("nope", SessionStatus.FAILED)

    def test_terminal_states_have_no_transitions(self) -> None:
        assert len(VALID_TRANSITIONS[SessionStatus.COMPLETED]) == 0
        assert len(VALID_TRANSITIONS[SessionStatus.FAILED]) == 0

    @pytest.mark.asyncio
    async def test_running_to_waiting_for_tool(
        self, store: SQLiteEventStore
    ) -> None:
        await store.create_session("s", agent="a")
        await store.update_status("s", SessionStatus.WAITING_FOR_TOOL)
        rec = await store.get_session("s")
        assert rec is not None
        assert rec.status == SessionStatus.WAITING_FOR_TOOL

    @pytest.mark.asyncio
    async def test_waiting_for_tool_to_running(
        self, store: SQLiteEventStore
    ) -> None:
        await store.create_session("s", agent="a")
        await store.update_status("s", SessionStatus.WAITING_FOR_TOOL)
        await store.update_status("s", SessionStatus.RUNNING)
        rec = await store.get_session("s")
        assert rec is not None
        assert rec.status == SessionStatus.RUNNING


# ---------------------------------------------------------------------------
# Event append + replay
# ---------------------------------------------------------------------------


class TestEventLog:
    @pytest.mark.asyncio
    async def test_append_and_get(self, store: SQLiteEventStore) -> None:
        await store.create_session("s", agent="a")

        event = AgentEvent(kind=AgentEventKind.TURN_START, turn=1)
        rec = await store.append("s", event)

        assert rec.session_id == "s"
        assert rec.seq == 1
        assert rec.kind == AgentEventKind.TURN_START

        events = await store.get_events("s")
        assert len(events) == 1
        assert events[0].seq == 1

    @pytest.mark.asyncio
    async def test_seq_increments(self, store: SQLiteEventStore) -> None:
        await store.create_session("s", agent="a")
        await store.append("s", AgentEvent(kind=AgentEventKind.TURN_START, turn=1))
        await store.append(
            "s", AgentEvent(kind=AgentEventKind.TEXT_DELTA, text="hi", turn=1)
        )
        await store.append(
            "s", AgentEvent(kind=AgentEventKind.TURN_COMPLETE, turn=1)
        )

        events = await store.get_events("s")
        assert [e.seq for e in events] == [1, 2, 3]

    @pytest.mark.asyncio
    async def test_after_seq_filter(self, store: SQLiteEventStore) -> None:
        await store.create_session("s", agent="a")
        for i in range(5):
            await store.append(
                "s",
                AgentEvent(kind=AgentEventKind.TEXT_DELTA, text=str(i), turn=1),
            )

        events = await store.get_events("s", after_seq=3)
        assert len(events) == 2
        assert events[0].seq == 4
        assert events[1].seq == 5

    @pytest.mark.asyncio
    async def test_payload_round_trip(self, store: SQLiteEventStore) -> None:
        await store.create_session("s", agent="a")

        event = AgentEvent(
            kind=AgentEventKind.TOOL_RESULT,
            tool_name="search",
            tool_result='{"hits": 42}',
            is_error=False,
            turn=2,
        )
        await store.append("s", event)

        events = await store.get_events("s")
        assert len(events) == 1
        payload = events[0].payload
        assert payload["tool_name"] == "search"
        assert payload["is_error"] is False
        assert payload["turn"] == 2

    @pytest.mark.asyncio
    async def test_empty_session_returns_no_events(
        self, store: SQLiteEventStore
    ) -> None:
        await store.create_session("s", agent="a")
        events = await store.get_events("s")
        assert events == []

    @pytest.mark.asyncio
    async def test_events_isolated_between_sessions(
        self, store: SQLiteEventStore
    ) -> None:
        await store.create_session("a", agent="x")
        await store.create_session("b", agent="y")
        await store.append(
            "a", AgentEvent(kind=AgentEventKind.TEXT_DELTA, text="a", turn=1)
        )
        await store.append(
            "b", AgentEvent(kind=AgentEventKind.TEXT_DELTA, text="b", turn=1)
        )

        a_events = await store.get_events("a")
        b_events = await store.get_events("b")
        assert len(a_events) == 1
        assert len(b_events) == 1
        assert a_events[0].payload["text"] == "a"
        assert b_events[0].payload["text"] == "b"
