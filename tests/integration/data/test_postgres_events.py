"""Integration tests for :class:`PostgresEventRepo` against a real Postgres.

Uses testcontainers to spin up Postgres 16 once per session and runs
the same lifecycle the SQLite tests cover: create_session, append,
get_events, list_sessions, status transitions, sequence ordering,
metadata round-trip.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from obscura.core.enums.agent import AgentEventKind
from obscura.core.enums.lifecycle import SessionStatus
from obscura.core.types import AgentEvent
from obscura.data.events.postgres import PostgresEventRepo


pytestmark = pytest.mark.integration


def _make_event(
    text: str = "hello",
    kind: AgentEventKind = AgentEventKind.TEXT_DELTA,
) -> AgentEvent:
    return AgentEvent(kind=kind, text=text, turn=1)


class TestPostgresEventRepoLifecycle:
    def test_create_and_get_session(self, pg_env: dict[str, Any]) -> None:
        del pg_env  # fixture sets the env, repo reads from it
        repo = PostgresEventRepo()
        sess = asyncio.run(
            repo.create_session(
                "sess-create",
                agent="oncall",
                backend="claude",
                model="claude-sonnet-4-6",
                summary="initial",
            ),
        )
        assert sess.id == "sess-create"
        assert sess.status == SessionStatus.RUNNING

        fetched = asyncio.run(repo.get_session("sess-create"))
        assert fetched is not None
        assert fetched.id == "sess-create"
        assert fetched.backend == "claude"
        assert fetched.summary == "initial"

    def test_get_missing_session_returns_none(
        self,
        pg_env: dict[str, Any],
    ) -> None:
        del pg_env
        repo = PostgresEventRepo()
        assert asyncio.run(repo.get_session("does-not-exist")) is None

    def test_append_assigns_monotonic_seq(self, pg_env: dict[str, Any]) -> None:
        del pg_env
        repo = PostgresEventRepo()
        asyncio.run(repo.create_session("sess-seq", agent="a"))

        e1 = asyncio.run(repo.append("sess-seq", _make_event("first")))
        e2 = asyncio.run(repo.append("sess-seq", _make_event("second")))
        e3 = asyncio.run(repo.append("sess-seq", _make_event("third")))

        assert e1.seq == 1
        assert e2.seq == 2
        assert e3.seq == 3

    def test_get_events_after_filters_by_seq(
        self,
        pg_env: dict[str, Any],
    ) -> None:
        del pg_env
        repo = PostgresEventRepo()
        asyncio.run(repo.create_session("sess-events", agent="a"))
        for i in range(5):
            asyncio.run(repo.append("sess-events", _make_event(f"e{i}")))

        all_events = asyncio.run(repo.get_events("sess-events"))
        after_two = asyncio.run(repo.get_events("sess-events", after_seq=2))
        assert len(all_events) == 5
        assert len(after_two) == 3
        assert [e.seq for e in after_two] == [3, 4, 5]

    def test_status_transition_valid(self, pg_env: dict[str, Any]) -> None:
        del pg_env
        repo = PostgresEventRepo()
        asyncio.run(repo.create_session("sess-status", agent="a"))
        asyncio.run(
            repo.update_status("sess-status", SessionStatus.COMPLETED),
        )
        sess = asyncio.run(repo.get_session("sess-status"))
        assert sess is not None
        assert sess.status == SessionStatus.COMPLETED

    def test_status_transition_invalid_raises(
        self,
        pg_env: dict[str, Any],
    ) -> None:
        del pg_env
        repo = PostgresEventRepo()
        asyncio.run(repo.create_session("sess-bad", agent="a"))
        asyncio.run(repo.update_status("sess-bad", SessionStatus.COMPLETED))
        with pytest.raises(ValueError, match="Invalid transition"):
            asyncio.run(repo.update_status("sess-bad", SessionStatus.RUNNING))

    def test_update_session_metadata_round_trip(
        self,
        pg_env: dict[str, Any],
    ) -> None:
        del pg_env
        repo = PostgresEventRepo()
        asyncio.run(repo.create_session("sess-meta", agent="a"))
        asyncio.run(
            repo.update_session(
                "sess-meta",
                summary="updated",
                message_count=7,
                metadata={"branch": "main", "tag": "v1"},
            ),
        )
        sess = asyncio.run(repo.get_session("sess-meta"))
        assert sess is not None
        assert sess.summary == "updated"
        assert sess.message_count == 7
        assert sess.metadata == {"branch": "main", "tag": "v1"}

    def test_list_sessions_filters(self, pg_env: dict[str, Any]) -> None:
        del pg_env
        repo = PostgresEventRepo()
        asyncio.run(
            repo.create_session("a", agent="x", backend="claude", source="live"),
        )
        asyncio.run(
            repo.create_session("b", agent="x", backend="copilot", source="live"),
        )
        asyncio.run(
            repo.create_session(
                "c",
                agent="x",
                backend="claude",
                source="replay",
            ),
        )

        all_sessions = asyncio.run(repo.list_sessions())
        claude = asyncio.run(repo.list_sessions(backend="claude"))
        live = asyncio.run(repo.list_sessions(source="live"))

        assert len(all_sessions) == 3
        assert {s.id for s in claude} == {"a", "c"}
        assert {s.id for s in live} == {"a", "b"}

    def test_event_payload_round_trip(self, pg_env: dict[str, Any]) -> None:
        del pg_env
        repo = PostgresEventRepo()
        asyncio.run(repo.create_session("sess-payload", agent="a"))
        evt = AgentEvent(
            kind=AgentEventKind.TOOL_CALL,
            tool_name="grep_files",
            tool_input={"pattern": "TODO"},
            turn=2,
        )
        appended = asyncio.run(repo.append("sess-payload", evt))
        assert appended.kind == AgentEventKind.TOOL_CALL

        events = asyncio.run(repo.get_events("sess-payload"))
        assert len(events) == 1
        assert events[0].kind == AgentEventKind.TOOL_CALL
        assert events[0].payload.get("tool_name") == "grep_files"
        assert events[0].payload.get("tool_input") == {"pattern": "TODO"}

    def test_factory_picks_postgres_when_env_set(
        self,
        pg_env: dict[str, Any],
    ) -> None:
        del pg_env
        from obscura.data.events.factory import get_event_repo

        repo = get_event_repo()
        # Factory routes to Postgres because OBSCURA_PG_HOST is set.
        from obscura.data.events.postgres import PostgresEventRepo as _PEX

        assert isinstance(repo, _PEX)

    def test_factory_explicit_path_overrides_env(
        self,
        pg_env: dict[str, Any],
        tmp_path: Any,
    ) -> None:
        del pg_env
        from obscura.data.events.factory import get_event_repo
        from obscura.data.events.sqlite import SqliteEventRepo

        repo = get_event_repo(tmp_path / "events.db")
        assert isinstance(repo, SqliteEventRepo)
