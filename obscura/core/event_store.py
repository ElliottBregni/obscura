"""
obscura.core.event_store — Durable event-sourced session persistence.

Every event emitted by the agent loop is appended to an immutable log.
Sessions are recovered by replaying events.  The store is the single
source of truth for session state.

Usage::

    from obscura.core.event_store import SQLiteEventStore, SessionStatus

    store = SQLiteEventStore("/tmp/events.db")
    await store.create_session("sess-1", agent="oncall")
    await store.append("sess-1", event)
    events = await store.get_events("sess-1")
"""

from __future__ import annotations

import asyncio
import enum
import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from obscura.core.types import AgentEvent, AgentEventKind


# ---------------------------------------------------------------------------
# Session status machine
# ---------------------------------------------------------------------------


class SessionStatus(enum.Enum):
    """Lifecycle states for a durable session."""

    RUNNING = "running"
    WAITING_FOR_TOOL = "waiting_for_tool"
    WAITING_FOR_USER = "waiting_for_user"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"


VALID_TRANSITIONS: dict[SessionStatus, frozenset[SessionStatus]] = {
    SessionStatus.RUNNING: frozenset(
        {
            SessionStatus.WAITING_FOR_TOOL,
            SessionStatus.WAITING_FOR_USER,
            SessionStatus.PAUSED,
            SessionStatus.COMPLETED,
            SessionStatus.FAILED,
        }
    ),
    SessionStatus.WAITING_FOR_TOOL: frozenset(
        {
            SessionStatus.RUNNING,
            SessionStatus.PAUSED,
            SessionStatus.FAILED,
        }
    ),
    SessionStatus.WAITING_FOR_USER: frozenset(
        {
            SessionStatus.RUNNING,
            SessionStatus.PAUSED,
            SessionStatus.FAILED,
        }
    ),
    SessionStatus.PAUSED: frozenset(
        {
            SessionStatus.RUNNING,
            SessionStatus.FAILED,
        }
    ),
    SessionStatus.COMPLETED: frozenset(),
    SessionStatus.FAILED: frozenset(),
}


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionRecord:
    """Persistent session metadata."""

    id: str
    status: SessionStatus
    active_agent: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class EventRecord:
    """A single persisted event in the append-only log."""

    session_id: str
    seq: int
    kind: AgentEventKind
    payload: dict[str, Any]
    timestamp: datetime


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class EventStoreProtocol(Protocol):
    """Abstract event store — swap SQLite for Postgres/Redis later."""

    async def create_session(
        self,
        session_id: str,
        agent: str,
    ) -> SessionRecord: ...

    async def get_session(self, session_id: str) -> SessionRecord | None: ...

    async def update_status(
        self,
        session_id: str,
        status: SessionStatus,
    ) -> None: ...

    async def append(
        self,
        session_id: str,
        event: AgentEvent,
    ) -> EventRecord: ...

    async def get_events(
        self,
        session_id: str,
        *,
        after_seq: int = 0,
    ) -> list[EventRecord]: ...

    async def list_sessions(
        self,
        *,
        status: SessionStatus | None = None,
    ) -> list[SessionRecord]: ...


# ---------------------------------------------------------------------------
# SQLite implementation
# ---------------------------------------------------------------------------


def _serialize_event(event: AgentEvent) -> str:
    """Serialize an AgentEvent to a JSON payload string."""
    payload: dict[str, Any] = {
        "kind": event.kind.value,
        "text": event.text,
        "tool_name": event.tool_name,
        "tool_input": event.tool_input,
        "tool_result": event.tool_result,
        "tool_use_id": event.tool_use_id,
        "is_error": event.is_error,
        "turn": event.turn,
    }
    return json.dumps(payload, default=str)


def _deserialize_payload(raw: str) -> dict[str, Any]:
    """Deserialize a JSON payload string."""
    from typing import cast

    result: object = json.loads(raw)
    if not isinstance(result, dict):
        return {}
    return cast(dict[str, Any], result)


class SQLiteEventStore:
    """File-backed event store using SQLite.

    Thread-safe.  All public methods are async (run DB ops via
    ``asyncio.to_thread`` to avoid blocking the event loop).
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._init_schema()

    # -- connection management -----------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            conn = sqlite3.connect(str(self._db_path))
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
        return self._local.conn

    def _init_schema(self) -> None:
        conn = self._conn()
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id            TEXT PRIMARY KEY,
                status        TEXT NOT NULL DEFAULT 'running',
                active_agent  TEXT NOT NULL DEFAULT '',
                created_at    TEXT NOT NULL,
                updated_at    TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS events (
                session_id  TEXT    NOT NULL,
                seq         INTEGER NOT NULL,
                kind        TEXT    NOT NULL,
                payload     TEXT    NOT NULL,
                timestamp   TEXT    NOT NULL,
                PRIMARY KEY (session_id, seq),
                FOREIGN KEY (session_id) REFERENCES sessions(id)
            );

            CREATE INDEX IF NOT EXISTS idx_events_session
                ON events(session_id, seq);
            """
        )
        conn.commit()

    # -- sync helpers (run in thread) ----------------------------------------

    def _create_session_sync(
        self,
        session_id: str,
        agent: str,
    ) -> SessionRecord:
        now = datetime.now(UTC).isoformat()
        conn = self._conn()
        conn.execute(
            "INSERT INTO sessions (id, status, active_agent, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (session_id, SessionStatus.RUNNING.value, agent, now, now),
        )
        conn.commit()
        return SessionRecord(
            id=session_id,
            status=SessionStatus.RUNNING,
            active_agent=agent,
            created_at=datetime.fromisoformat(now),
            updated_at=datetime.fromisoformat(now),
        )

    def _get_session_sync(self, session_id: str) -> SessionRecord | None:
        row = self._conn().execute(
            "SELECT id, status, active_agent, created_at, updated_at "
            "FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        return SessionRecord(
            id=row["id"],
            status=SessionStatus(row["status"]),
            active_agent=row["active_agent"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def _update_status_sync(
        self,
        session_id: str,
        status: SessionStatus,
    ) -> None:
        conn = self._conn()
        row = conn.execute(
            "SELECT status FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"Session not found: {session_id}")

        current = SessionStatus(row["status"])
        if status not in VALID_TRANSITIONS[current]:
            raise ValueError(
                f"Invalid transition: {current.value} -> {status.value}"
            )

        now = datetime.now(UTC).isoformat()
        conn.execute(
            "UPDATE sessions SET status = ?, updated_at = ? WHERE id = ?",
            (status.value, now, session_id),
        )
        conn.commit()

    def _append_sync(
        self,
        session_id: str,
        event: AgentEvent,
    ) -> EventRecord:
        conn = self._conn()
        row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) AS max_seq FROM events WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        seq = row["max_seq"] + 1
        now = datetime.now(UTC).isoformat()
        payload = _serialize_event(event)

        conn.execute(
            "INSERT INTO events (session_id, seq, kind, payload, timestamp) "
            "VALUES (?, ?, ?, ?, ?)",
            (session_id, seq, event.kind.value, payload, now),
        )
        conn.commit()

        return EventRecord(
            session_id=session_id,
            seq=seq,
            kind=event.kind,
            payload=_deserialize_payload(payload),
            timestamp=datetime.fromisoformat(now),
        )

    def _get_events_sync(
        self,
        session_id: str,
        after_seq: int = 0,
    ) -> list[EventRecord]:
        rows = self._conn().execute(
            "SELECT session_id, seq, kind, payload, timestamp "
            "FROM events WHERE session_id = ? AND seq > ? ORDER BY seq",
            (session_id, after_seq),
        ).fetchall()
        return [
            EventRecord(
                session_id=row["session_id"],
                seq=row["seq"],
                kind=AgentEventKind(row["kind"]),
                payload=_deserialize_payload(row["payload"]),
                timestamp=datetime.fromisoformat(row["timestamp"]),
            )
            for row in rows
        ]

    def _list_sessions_sync(
        self,
        status: SessionStatus | None = None,
    ) -> list[SessionRecord]:
        conn = self._conn()
        if status is not None:
            rows = conn.execute(
                "SELECT id, status, active_agent, created_at, updated_at "
                "FROM sessions WHERE status = ? ORDER BY updated_at DESC",
                (status.value,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, status, active_agent, created_at, updated_at "
                "FROM sessions ORDER BY updated_at DESC"
            ).fetchall()
        return [
            SessionRecord(
                id=row["id"],
                status=SessionStatus(row["status"]),
                active_agent=row["active_agent"],
                created_at=datetime.fromisoformat(row["created_at"]),
                updated_at=datetime.fromisoformat(row["updated_at"]),
            )
            for row in rows
        ]

    # -- async public API ----------------------------------------------------

    async def create_session(
        self,
        session_id: str,
        agent: str,
    ) -> SessionRecord:
        return await asyncio.to_thread(self._create_session_sync, session_id, agent)

    async def get_session(self, session_id: str) -> SessionRecord | None:
        return await asyncio.to_thread(self._get_session_sync, session_id)

    async def update_status(
        self,
        session_id: str,
        status: SessionStatus,
    ) -> None:
        await asyncio.to_thread(self._update_status_sync, session_id, status)

    async def append(
        self,
        session_id: str,
        event: AgentEvent,
    ) -> EventRecord:
        return await asyncio.to_thread(self._append_sync, session_id, event)

    async def get_events(
        self,
        session_id: str,
        *,
        after_seq: int = 0,
    ) -> list[EventRecord]:
        return await asyncio.to_thread(self._get_events_sync, session_id, after_seq)

    async def list_sessions(
        self,
        *,
        status: SessionStatus | None = None,
    ) -> list[SessionRecord]:
        return await asyncio.to_thread(self._list_sessions_sync, status)

    def close(self) -> None:
        """Close the thread-local connection."""
        if hasattr(self._local, "conn") and self._local.conn is not None:
            self._local.conn.close()
            self._local.conn = None
