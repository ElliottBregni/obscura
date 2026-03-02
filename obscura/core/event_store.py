"""
obscura.core.event_store — Durable event-sourced session persistence.

Every event emitted by the agent loop is appended to an immutable log.
Sessions are recovered by replaying events.  The store is the single
source of truth for session state.

Usage::

    from obscura.core.event_store import SQLiteEventStore, SessionStatus

    store = SQLiteEventStore("/tmp/events.db")
    await store.create_session("sess-1", agent="oncall", backend="claude", model="claude-sonnet-4-5-20250929")
    await store.append("sess-1", event)
    events = await store.get_events("sess-1")
"""

from __future__ import annotations

import asyncio
import enum
import json
import sqlite3
import threading
from dataclasses import dataclass, field
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


def _empty_dict() -> dict[str, Any]:
    return {}


@dataclass(frozen=True)
class SessionRecord:
    """Persistent session metadata."""

    id: str
    status: SessionStatus
    backend: str = ""
    model: str = ""
    active_agent: str = ""
    source: str = "live"
    project: str = ""
    summary: str = ""
    message_count: int = 0
    metadata: dict[str, Any] = field(default_factory=_empty_dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))


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
        *,
        backend: str = "",
        model: str = "",
        source: str = "live",
        project: str = "",
        summary: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> SessionRecord: ...

    async def get_session(self, session_id: str) -> SessionRecord | None: ...

    async def update_status(
        self,
        session_id: str,
        status: SessionStatus,
    ) -> None: ...

    async def update_session(
        self,
        session_id: str,
        *,
        summary: str | None = None,
        message_count: int | None = None,
        metadata: dict[str, Any] | None = None,
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
        backend: str | None = None,
        source: str | None = None,
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


_SESSION_COLS = (
    "id, status, backend, model, active_agent, source, project, "
    "summary, message_count, metadata, created_at, updated_at"
)


def _row_to_session(row: sqlite3.Row) -> SessionRecord:
    """Convert a DB row to a SessionRecord."""
    raw_meta = row["metadata"]
    meta: dict[str, Any] = {}
    if raw_meta:
        try:
            parsed = json.loads(raw_meta)
            if isinstance(parsed, dict):
                meta = parsed
        except (json.JSONDecodeError, TypeError):
            pass
    return SessionRecord(
        id=row["id"],
        status=SessionStatus(row["status"]),
        backend=row["backend"] or "",
        model=row["model"] or "",
        active_agent=row["active_agent"] or "",
        source=row["source"] or "live",
        project=row["project"] or "",
        summary=row["summary"] or "",
        message_count=int(row["message_count"] or 0),
        metadata=meta,
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


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

        # Migrate: add unified session columns (idempotent)
        _migrations: list[tuple[str, str]] = [
            ("backend", "TEXT NOT NULL DEFAULT ''"),
            ("model", "TEXT NOT NULL DEFAULT ''"),
            ("source", "TEXT NOT NULL DEFAULT 'live'"),
            ("project", "TEXT NOT NULL DEFAULT ''"),
            ("summary", "TEXT NOT NULL DEFAULT ''"),
            ("message_count", "INTEGER NOT NULL DEFAULT 0"),
            ("metadata", "TEXT NOT NULL DEFAULT '{}'"),
        ]
        for col_name, col_def in _migrations:
            try:
                conn.execute(
                    f"ALTER TABLE sessions ADD COLUMN {col_name} {col_def}"
                )
            except sqlite3.OperationalError:
                pass  # column already exists

        # Add indexes for new columns
        for idx_sql in [
            "CREATE INDEX IF NOT EXISTS idx_sessions_backend ON sessions(backend)",
            "CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status)",
            "CREATE INDEX IF NOT EXISTS idx_sessions_source ON sessions(source)",
            "CREATE INDEX IF NOT EXISTS idx_sessions_updated ON sessions(updated_at DESC)",
        ]:
            conn.execute(idx_sql)
        conn.commit()

    # -- sync helpers (run in thread) ----------------------------------------

    def _create_session_sync(
        self,
        session_id: str,
        agent: str,
        *,
        backend: str = "",
        model: str = "",
        source: str = "live",
        project: str = "",
        summary: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> SessionRecord:
        now = datetime.now(UTC).isoformat()
        meta_json = json.dumps(metadata or {}, default=str)
        conn = self._conn()
        conn.execute(
            "INSERT INTO sessions "
            "(id, status, backend, model, active_agent, source, project, "
            " summary, message_count, metadata, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)",
            (
                session_id,
                SessionStatus.RUNNING.value,
                backend,
                model,
                agent,
                source,
                project,
                summary,
                meta_json,
                now,
                now,
            ),
        )
        conn.commit()
        return SessionRecord(
            id=session_id,
            status=SessionStatus.RUNNING,
            backend=backend,
            model=model,
            active_agent=agent,
            source=source,
            project=project,
            summary=summary,
            message_count=0,
            metadata=metadata or {},
            created_at=datetime.fromisoformat(now),
            updated_at=datetime.fromisoformat(now),
        )

    def _get_session_sync(self, session_id: str) -> SessionRecord | None:
        row = self._conn().execute(
            f"SELECT {_SESSION_COLS} FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        return _row_to_session(row)

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

    def _update_session_sync(
        self,
        session_id: str,
        *,
        summary: str | None = None,
        message_count: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        conn = self._conn()
        sets: list[str] = []
        params: list[Any] = []

        if summary is not None:
            sets.append("summary = ?")
            params.append(summary)
        if message_count is not None:
            sets.append("message_count = ?")
            params.append(message_count)
        if metadata is not None:
            sets.append("metadata = ?")
            params.append(json.dumps(metadata, default=str))

        if not sets:
            return

        sets.append("updated_at = ?")
        params.append(datetime.now(UTC).isoformat())
        params.append(session_id)

        conn.execute(
            f"UPDATE sessions SET {', '.join(sets)} WHERE id = ?",
            params,
        )
        conn.commit()

    def _append_sync(
        self,
        session_id: str,
        event: AgentEvent,
    ) -> EventRecord:
        conn = self._conn()
        # BEGIN IMMEDIATE serialises concurrent writers — prevents
        # two tasks reading the same MAX(seq) and colliding on INSERT.
        conn.execute("BEGIN IMMEDIATE")
        try:
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
        except Exception:
            conn.rollback()
            raise

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
        backend: str | None = None,
        source: str | None = None,
    ) -> list[SessionRecord]:
        conn = self._conn()
        clauses: list[str] = []
        params: list[Any] = []

        if status is not None:
            clauses.append("status = ?")
            params.append(status.value)
        if backend is not None:
            clauses.append("backend = ?")
            params.append(backend)
        if source is not None:
            clauses.append("source = ?")
            params.append(source)

        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = conn.execute(
            f"SELECT {_SESSION_COLS} FROM sessions{where} ORDER BY updated_at DESC",
            params,
        ).fetchall()
        return [_row_to_session(row) for row in rows]

    # -- async public API ----------------------------------------------------

    async def create_session(
        self,
        session_id: str,
        agent: str,
        *,
        backend: str = "",
        model: str = "",
        source: str = "live",
        project: str = "",
        summary: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> SessionRecord:
        return await asyncio.to_thread(
            self._create_session_sync,
            session_id,
            agent,
            backend=backend,
            model=model,
            source=source,
            project=project,
            summary=summary,
            metadata=metadata,
        )

    async def get_session(self, session_id: str) -> SessionRecord | None:
        return await asyncio.to_thread(self._get_session_sync, session_id)

    async def update_status(
        self,
        session_id: str,
        status: SessionStatus,
    ) -> None:
        await asyncio.to_thread(self._update_status_sync, session_id, status)

    async def update_session(
        self,
        session_id: str,
        *,
        summary: str | None = None,
        message_count: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        await asyncio.to_thread(
            self._update_session_sync,
            session_id,
            summary=summary,
            message_count=message_count,
            metadata=metadata,
        )

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
        backend: str | None = None,
        source: str | None = None,
    ) -> list[SessionRecord]:
        return await asyncio.to_thread(
            self._list_sessions_sync, status, backend, source,
        )

    def close(self) -> None:
        """Close the thread-local connection."""
        if hasattr(self._local, "conn") and self._local.conn is not None:
            self._local.conn.close()
            self._local.conn = None
