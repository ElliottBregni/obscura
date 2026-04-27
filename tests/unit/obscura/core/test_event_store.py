"""Tests for obscura.core.event_store — Durable event-sourced session persistence."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from obscura.core.event_store import (
    VALID_TRANSITIONS,
    SessionStatus,
    SnapshotRecord,
    SQLiteEventStore,
)
from obscura.core.types import AgentEvent, AgentEventKind

if TYPE_CHECKING:
    from pathlib import Path


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
        self,
        store: SQLiteEventStore,
    ) -> None:
        await store.create_session("a", agent="x")
        await store.create_session("b", agent="y")
        await store.update_status("a", SessionStatus.COMPLETED)
        running = await store.list_sessions(status=SessionStatus.RUNNING)
        assert len(running) == 1
        assert running[0].id == "b"

    @pytest.mark.asyncio
    async def test_create_session_with_metadata(
        self,
        store: SQLiteEventStore,
    ) -> None:
        rec = await store.create_session(
            "sess-m",
            agent="oncall",
            backend="claude",
            model="claude-sonnet-4-5-20250929",
            source="live",
            project="myapp",
            summary="debugging auth",
            metadata={"git_branch": "main"},
        )
        assert rec.backend == "claude"
        assert rec.model == "claude-sonnet-4-5-20250929"
        assert rec.source == "live"
        assert rec.project == "myapp"
        assert rec.summary == "debugging auth"
        assert rec.metadata == {"git_branch": "main"}

    @pytest.mark.asyncio
    async def test_get_session_returns_new_fields(
        self,
        store: SQLiteEventStore,
    ) -> None:
        await store.create_session(
            "s",
            agent="a",
            backend="copilot",
            model="gpt-4o",
        )
        rec = await store.get_session("s")
        assert rec is not None
        assert rec.backend == "copilot"
        assert rec.model == "gpt-4o"
        assert rec.source == "live"

    @pytest.mark.asyncio
    async def test_list_sessions_filter_by_backend(
        self,
        store: SQLiteEventStore,
    ) -> None:
        await store.create_session("a", agent="x", backend="claude")
        await store.create_session("b", agent="y", backend="copilot")
        claude_sessions = await store.list_sessions(backend="claude")
        assert len(claude_sessions) == 1
        assert claude_sessions[0].id == "a"

    @pytest.mark.asyncio
    async def test_list_sessions_filter_by_source(
        self,
        store: SQLiteEventStore,
    ) -> None:
        await store.create_session("a", agent="x", source="live")
        await store.create_session("b", agent="y", source="ingested")
        ingested = await store.list_sessions(source="ingested")
        assert len(ingested) == 1
        assert ingested[0].id == "b"

    @pytest.mark.asyncio
    async def test_update_session_metadata(
        self,
        store: SQLiteEventStore,
    ) -> None:
        await store.create_session("s", agent="a")
        await store.update_session(
            "s",
            summary="new summary",
            message_count=42,
            metadata={"tools_used": ["search"]},
        )
        rec = await store.get_session("s")
        assert rec is not None
        assert rec.summary == "new summary"
        assert rec.message_count == 42
        assert rec.metadata == {"tools_used": ["search"]}

    @pytest.mark.asyncio
    async def test_old_sessions_get_defaults(
        self,
        store: SQLiteEventStore,
    ) -> None:
        """Sessions created without new fields should have sensible defaults."""
        rec = await store.create_session("old", agent="a")
        assert rec.backend == ""
        assert rec.model == ""
        assert rec.source == "live"
        assert rec.message_count == 0
        assert rec.metadata == {}


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
        self,
        store: SQLiteEventStore,
    ) -> None:
        await store.create_session("s", agent="a")
        await store.update_status("s", SessionStatus.WAITING_FOR_TOOL)
        rec = await store.get_session("s")
        assert rec is not None
        assert rec.status == SessionStatus.WAITING_FOR_TOOL

    @pytest.mark.asyncio
    async def test_waiting_for_tool_to_running(
        self,
        store: SQLiteEventStore,
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
            "s",
            AgentEvent(kind=AgentEventKind.TEXT_DELTA, text="hi", turn=1),
        )
        await store.append(
            "s",
            AgentEvent(kind=AgentEventKind.TURN_COMPLETE, turn=1),
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
        self,
        store: SQLiteEventStore,
    ) -> None:
        await store.create_session("s", agent="a")
        events = await store.get_events("s")
        assert events == []

    @pytest.mark.asyncio
    async def test_events_isolated_between_sessions(
        self,
        store: SQLiteEventStore,
    ) -> None:
        await store.create_session("a", agent="x")
        await store.create_session("b", agent="y")
        await store.append(
            "a",
            AgentEvent(kind=AgentEventKind.TEXT_DELTA, text="a", turn=1),
        )
        await store.append(
            "b",
            AgentEvent(kind=AgentEventKind.TEXT_DELTA, text="b", turn=1),
        )

        a_events = await store.get_events("a")
        b_events = await store.get_events("b")
        assert len(a_events) == 1
        assert len(b_events) == 1
        assert a_events[0].payload["text"] == "a"
        assert b_events[0].payload["text"] == "b"


# ---------------------------------------------------------------------------
# Fork & branching
# ---------------------------------------------------------------------------


async def _fill_events(store: SQLiteEventStore, sid: str, n: int) -> None:
    for i in range(n):
        await store.append(
            sid,
            AgentEvent(kind=AgentEventKind.TEXT_DELTA, text=f"{sid}-{i}", turn=1),
        )


class TestForkAndBranching:
    @pytest.mark.asyncio
    async def test_fork_creates_child_with_branched_at_seq(
        self,
        store: SQLiteEventStore,
    ) -> None:
        await store.create_session("p", agent="a", backend="claude", model="m")
        await _fill_events(store, "p", 5)
        child = await store.fork("p", 3, new_session_id="c", agent="a")
        assert child.id == "c"
        assert child.parent_session_id == "p"
        assert child.branched_at_seq == 3
        assert child.root_session_id == "p"
        assert child.backend == "claude"
        assert child.model == "m"

    @pytest.mark.asyncio
    async def test_fork_freezes_parent(self, store: SQLiteEventStore) -> None:
        await store.create_session("p", agent="a")
        await _fill_events(store, "p", 2)
        await store.fork("p", 2, new_session_id="c", agent="a")
        parent = await store.get_session("p")
        assert parent is not None
        assert parent.frozen is True

    @pytest.mark.asyncio
    async def test_fork_rejects_out_of_range(
        self,
        store: SQLiteEventStore,
    ) -> None:
        await store.create_session("p", agent="a")
        await _fill_events(store, "p", 3)
        with pytest.raises(ValueError, match="at_seq out of range"):
            await store.fork("p", 99, new_session_id="c", agent="a")
        with pytest.raises(ValueError, match="at_seq out of range"):
            await store.fork("p", -1, new_session_id="c2", agent="a")

    @pytest.mark.asyncio
    async def test_fork_unknown_parent(self, store: SQLiteEventStore) -> None:
        with pytest.raises(ValueError, match="parent not found"):
            await store.fork("nope", 0, new_session_id="c", agent="a")

    @pytest.mark.asyncio
    async def test_append_to_frozen_session_raises(
        self,
        store: SQLiteEventStore,
    ) -> None:
        await store.create_session("p", agent="a")
        await _fill_events(store, "p", 1)
        await store.fork("p", 1, new_session_id="c", agent="a")
        with pytest.raises(ValueError, match="session frozen"):
            await store.append(
                "p",
                AgentEvent(kind=AgentEventKind.TEXT_DELTA, text="x", turn=1),
            )

    @pytest.mark.asyncio
    async def test_freeze_session_explicit(self, store: SQLiteEventStore) -> None:
        await store.create_session("p", agent="a")
        await store.freeze_session("p")
        rec = await store.get_session("p")
        assert rec is not None
        assert rec.frozen is True
        with pytest.raises(ValueError, match="session frozen"):
            await store.append(
                "p",
                AgentEvent(kind=AgentEventKind.TEXT_DELTA, text="x", turn=1),
            )

    @pytest.mark.asyncio
    async def test_root_session_id_propagates_through_fork_of_fork(
        self,
        store: SQLiteEventStore,
    ) -> None:
        await store.create_session("root", agent="a")
        await _fill_events(store, "root", 3)
        await store.fork("root", 2, new_session_id="mid", agent="a")
        await _fill_events(store, "mid", 2)
        leaf = await store.fork("mid", 1, new_session_id="leaf", agent="a")
        assert leaf.root_session_id == "root"
        mid = await store.get_session("mid")
        assert mid is not None
        assert mid.root_session_id == "root"

    @pytest.mark.asyncio
    async def test_root_session_id_set_for_root(
        self,
        store: SQLiteEventStore,
    ) -> None:
        rec = await store.create_session("solo", agent="a")
        assert rec.root_session_id == "solo"

    @pytest.mark.asyncio
    async def test_materialize_events_concatenates_chain(
        self,
        store: SQLiteEventStore,
    ) -> None:
        await store.create_session("p", agent="a")
        await _fill_events(store, "p", 4)
        await store.fork("p", 2, new_session_id="c", agent="a")
        await _fill_events(store, "c", 3)

        events = await store.materialize_events("c")
        # Parent prefix (seq 1, 2) + leaf events (seq 1, 2, 3)
        assert len(events) == 5
        assert [e.session_id for e in events] == ["p", "p", "c", "c", "c"]
        assert events[0].payload["text"] == "p-0"
        assert events[1].payload["text"] == "p-1"
        assert events[2].payload["text"] == "c-0"

    @pytest.mark.asyncio
    async def test_materialize_events_root_only(
        self,
        store: SQLiteEventStore,
    ) -> None:
        await store.create_session("root", agent="a")
        await _fill_events(store, "root", 3)
        events = await store.materialize_events("root")
        assert len(events) == 3
        assert all(e.session_id == "root" for e in events)


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------


class TestSnapshots:
    @pytest.mark.asyncio
    async def test_snapshot_round_trip(self, store: SQLiteEventStore) -> None:
        await store.create_session("s", agent="a")
        await _fill_events(store, "s", 4)
        snap = await store.write_snapshot("s", 4, '{"summary": "hi"}')
        assert snap.session_id == "s"
        assert snap.up_to_seq == 4
        assert snap.context_blob == '{"summary": "hi"}'
        assert snap.format_version == 1

        snaps = await store.list_snapshots("s")
        assert len(snaps) == 1
        assert snaps[0].context_blob == '{"summary": "hi"}'

    @pytest.mark.asyncio
    async def test_get_nearest_snapshot_none_when_empty(
        self,
        store: SQLiteEventStore,
    ) -> None:
        await store.create_session("s", agent="a")
        result = await store.get_nearest_snapshot("s")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_nearest_snapshot_prefers_descendant(
        self,
        store: SQLiteEventStore,
    ) -> None:
        await store.create_session("p", agent="a")
        await _fill_events(store, "p", 3)
        await store.write_snapshot("p", 2, "parent-snap")
        await store.fork("p", 3, new_session_id="c", agent="a")
        await _fill_events(store, "c", 2)
        await store.write_snapshot("c", 2, "child-snap")

        result = await store.get_nearest_snapshot("c")
        assert result is not None
        assert result.session_id == "c"
        assert result.context_blob == "child-snap"

    @pytest.mark.asyncio
    async def test_get_nearest_snapshot_falls_back_to_ancestor(
        self,
        store: SQLiteEventStore,
    ) -> None:
        await store.create_session("p", agent="a")
        await _fill_events(store, "p", 3)
        await store.write_snapshot("p", 2, "parent-snap")
        await store.fork("p", 3, new_session_id="c", agent="a")
        await _fill_events(store, "c", 2)

        result = await store.get_nearest_snapshot("c")
        assert result is not None
        assert result.session_id == "p"
        assert result.context_blob == "parent-snap"

    @pytest.mark.asyncio
    async def test_get_nearest_snapshot_honors_max_seq(
        self,
        store: SQLiteEventStore,
    ) -> None:
        await store.create_session("s", agent="a")
        await _fill_events(store, "s", 10)
        await store.write_snapshot("s", 3, "snap-3")
        await store.write_snapshot("s", 7, "snap-7")

        result = await store.get_nearest_snapshot("s", max_seq=5)
        assert result is not None
        assert result.up_to_seq == 3

        result_full = await store.get_nearest_snapshot("s")
        assert result_full is not None
        assert result_full.up_to_seq == 7

    @pytest.mark.asyncio
    async def test_get_nearest_snapshot_ancestor_respects_branched_at_seq(
        self,
        store: SQLiteEventStore,
    ) -> None:
        await store.create_session("p", agent="a")
        await _fill_events(store, "p", 5)
        # snapshot beyond the branch point should not be visible to child
        await store.write_snapshot("p", 4, "too-far")
        await store.write_snapshot("p", 2, "in-bounds")
        await store.fork("p", 3, new_session_id="c", agent="a")

        result = await store.get_nearest_snapshot("c")
        assert result is not None
        assert isinstance(result, SnapshotRecord)
        assert result.up_to_seq == 2
        assert result.context_blob == "in-bounds"


# ---------------------------------------------------------------------------
# Prefix materialization (SOC2 deletion path)
# ---------------------------------------------------------------------------


class TestPrefixMaterialization:
    @pytest.mark.asyncio
    async def test_inlines_parent_prefix_and_clears_pointer(
        self,
        store: SQLiteEventStore,
    ) -> None:
        await store.create_session("p", agent="a")
        await _fill_events(store, "p", 4)
        await store.fork("p", 2, new_session_id="c", agent="a")
        await _fill_events(store, "c", 3)

        n = await store.materialize_prefix_into_child("c")
        assert n == 2

        child = await store.get_session("c")
        assert child is not None
        assert child.parent_session_id == ""
        assert child.branched_at_seq == 0

        events = await store.get_events("c")
        assert len(events) == 5
        assert [e.seq for e in events] == [1, 2, 3, 4, 5]
        assert events[0].payload["text"] == "p-0"
        assert events[1].payload["text"] == "p-1"
        assert events[2].payload["text"] == "c-0"
        assert events[3].payload["text"] == "c-1"
        assert events[4].payload["text"] == "c-2"

    @pytest.mark.asyncio
    async def test_root_session_is_noop(self, store: SQLiteEventStore) -> None:
        await store.create_session("root", agent="a")
        await _fill_events(store, "root", 2)
        n = await store.materialize_prefix_into_child("root")
        assert n == 0
        events = await store.get_events("root")
        assert len(events) == 2

    @pytest.mark.asyncio
    async def test_inlines_grandparent_chain(
        self,
        store: SQLiteEventStore,
    ) -> None:
        await store.create_session("g", agent="a")
        await _fill_events(store, "g", 3)
        await store.fork("g", 2, new_session_id="m", agent="a")
        await _fill_events(store, "m", 2)
        await store.fork("m", 2, new_session_id="leaf", agent="a")
        await _fill_events(store, "leaf", 1)

        n = await store.materialize_prefix_into_child("leaf")
        # Grandparent prefix (2) + parent (now-flattened: 2 + 2 = 4) prefix.
        assert n == 4
        leaf = await store.get_session("leaf")
        assert leaf is not None
        assert leaf.parent_session_id == ""
        assert leaf.branched_at_seq == 0
        events = await store.get_events("leaf")
        assert len(events) == 5
