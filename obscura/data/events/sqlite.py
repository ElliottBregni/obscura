"""SQLite implementation of the :class:`EventRepo` Protocol.

Schema, queries, and connection management for durable event-sourced
session persistence. WAL mode + thread-local connections; sync helpers
run via ``asyncio.to_thread`` so the public API can stay async without
blocking the event loop.

This is the post-migration home for what used to be
``obscura.core.event_store.SQLiteEventStore`` — that module is now a
thin re-export shim. Behaviour is byte-for-byte identical; only the
class name changed (``SQLiteEventStore`` → ``SqliteEventRepo``) and the
SQL was lifted into module-level constants so a future Postgres backend
can mirror the same query surface.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
from collections.abc import Mapping
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from obscura.core.enums.agent import AgentEventKind
from obscura.core.enums.lifecycle import (
    SESSION_VALID_TRANSITIONS as VALID_TRANSITIONS,
)
from obscura.core.enums.lifecycle import SessionStatus
from obscura.core.session_utils import list_active_sessions
from obscura.core.types import AgentEvent
from obscura.data.events.protocol import EventRecord, SessionRecord

logger = logging.getLogger(__name__)


_SESSION_COLS = (
    "id, status, backend, model, active_agent, source, parent_session_id, project, "
    "summary, message_count, metadata, created_at, updated_at"
)


_SCHEMA = """
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


# Idempotent ALTER TABLEs to evolve schema across releases.
_MIGRATIONS: list[tuple[str, str]] = [
    ("backend", "TEXT NOT NULL DEFAULT ''"),
    ("model", "TEXT NOT NULL DEFAULT ''"),
    ("source", "TEXT NOT NULL DEFAULT 'live'"),
    ("project", "TEXT NOT NULL DEFAULT ''"),
    ("summary", "TEXT NOT NULL DEFAULT ''"),
    ("message_count", "INTEGER NOT NULL DEFAULT 0"),
    ("metadata", "TEXT NOT NULL DEFAULT '{}'"),
    ("parent_session_id", "TEXT NOT NULL DEFAULT ''"),
]


_INDEX_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_sessions_backend ON sessions(backend)",
    "CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status)",
    "CREATE INDEX IF NOT EXISTS idx_sessions_source ON sessions(source)",
    "CREATE INDEX IF NOT EXISTS idx_sessions_updated ON sessions(updated_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_sessions_parent ON sessions(parent_session_id)",
)


_QUERIES = {
    "create_session": (
        "INSERT INTO sessions "
        "(id, status, backend, model, active_agent, source, parent_session_id, "
        " project, summary, message_count, metadata, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)"
    ),
    "get_session": f"SELECT {_SESSION_COLS} FROM sessions WHERE id = ?",
    "get_session_status": "SELECT status FROM sessions WHERE id = ?",
    "update_status": "UPDATE sessions SET status = ?, updated_at = ? WHERE id = ?",
    "max_seq_for_session": (
        "SELECT COALESCE(MAX(seq), 0) AS max_seq FROM events WHERE session_id = ?"
    ),
    "insert_event": (
        "INSERT INTO events (session_id, seq, kind, payload, timestamp) "
        "VALUES (?, ?, ?, ?, ?)"
    ),
    "get_events_after": (
        "SELECT session_id, seq, kind, payload, timestamp "
        "FROM events WHERE session_id = ? AND seq > ? ORDER BY seq"
    ),
    "active_sessions_in": "SELECT id FROM sessions WHERE status IN ({placeholders})",
}


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
    if event.metadata is not None:
        payload["metadata"] = asdict(event.metadata)
    return json.dumps(payload, default=str)


def _deserialize_payload(raw: str) -> dict[str, Any]:
    """Deserialize a JSON payload string into a heterogeneous wire dict."""
    result: object = json.loads(raw)
    if not isinstance(result, dict):
        return {}
    return cast("dict[str, Any]", result)


def _row_to_session(row: sqlite3.Row) -> SessionRecord:
    """Convert a DB row to a SessionRecord."""
    return SessionRecord.from_row(row)


class SqliteEventRepo:
    """File-backed event repository using SQLite.

    Thread-safe. Public methods are async — DB ops dispatch through
    ``asyncio.to_thread`` so the event loop stays responsive.
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._init_schema()

    # -- connection management ----------------------------------------------

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
        conn.executescript(_SCHEMA)
        conn.commit()

        for col_name, col_def in _MIGRATIONS:
            try:
                conn.execute(
                    f"ALTER TABLE sessions ADD COLUMN {col_name} {col_def}",
                )
            except sqlite3.OperationalError:
                logger.debug("suppressed exception in _init_schema", exc_info=True)

        for idx_sql in _INDEX_DDL:
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
        parent_session_id: str = "",
        project: str = "",
        summary: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> SessionRecord:
        now = datetime.now(UTC).isoformat()
        meta_dict: dict[str, Any] = dict(metadata) if metadata else {}
        meta_json = json.dumps(meta_dict, default=str)
        conn = self._conn()
        conn.execute(
            _QUERIES["create_session"],
            (
                session_id,
                SessionStatus.RUNNING.value,
                backend,
                model,
                agent,
                source,
                parent_session_id,
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
            parent_session_id=parent_session_id,
            project=project,
            summary=summary,
            message_count=0,
            metadata=meta_dict,
            created_at=datetime.fromisoformat(now),
            updated_at=datetime.fromisoformat(now),
        )

    def _get_session_sync(self, session_id: str) -> SessionRecord | None:
        row = self._conn().execute(_QUERIES["get_session"], (session_id,)).fetchone()
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
            _QUERIES["get_session_status"],
            (session_id,),
        ).fetchone()
        if row is None:
            msg = f"Session not found: {session_id}"
            raise ValueError(msg)

        current = SessionStatus(row["status"])
        if status not in VALID_TRANSITIONS[current]:
            msg = f"Invalid transition: {current.value} -> {status.value}"
            raise ValueError(msg)

        now = datetime.now(UTC).isoformat()
        conn.execute(
            _QUERIES["update_status"],
            (status.value, now, session_id),
        )
        conn.commit()

    def _update_session_sync(
        self,
        session_id: str,
        *,
        summary: str | None = None,
        message_count: int | None = None,
        metadata: Mapping[str, Any] | None = None,
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
            params.append(json.dumps(dict(metadata), default=str))

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
                _QUERIES["max_seq_for_session"],
                (session_id,),
            ).fetchone()
            seq = row["max_seq"] + 1
            now = datetime.now(UTC).isoformat()
            payload = _serialize_event(event)

            conn.execute(
                _QUERIES["insert_event"],
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
        rows = (
            self._conn()
            .execute(_QUERIES["get_events_after"], (session_id, after_seq))
            .fetchall()
        )
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
        parent_session_id: str | None = None,
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
        if parent_session_id is not None:
            clauses.append("parent_session_id = ?")
            params.append(parent_session_id)

        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = conn.execute(
            f"SELECT {_SESSION_COLS} FROM sessions{where} ORDER BY updated_at DESC",
            params,
        ).fetchall()
        return [_row_to_session(row) for row in rows]

    def _reap_orphaned_sessions_sync(self) -> int:
        conn = self._conn()
        active_statuses = (
            SessionStatus.RUNNING,
            SessionStatus.WAITING_FOR_TOOL,
            SessionStatus.WAITING_FOR_USER,
        )
        placeholders = ",".join("?" * len(active_statuses))
        rows = conn.execute(
            _QUERIES["active_sessions_in"].format(placeholders=placeholders),
            [s.value for s in active_statuses],
        ).fetchall()

        if not rows:
            return 0

        alive_ids = {s.get("session_id", "")[:16] for s in list_active_sessions()}

        reaped = 0
        now = datetime.now(UTC).isoformat()
        for row in rows:
            sid = row["id"]
            if sid[:16] not in alive_ids:
                conn.execute(
                    _QUERIES["update_status"],
                    (SessionStatus.FAILED.value, now, sid),
                )
                reaped += 1

        if reaped:
            conn.commit()
        return reaped

    # -- async public API ----------------------------------------------------

    async def create_session(
        self,
        session_id: str,
        agent: str,
        *,
        backend: str = "",
        model: str = "",
        source: str = "live",
        parent_session_id: str = "",
        project: str = "",
        summary: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> SessionRecord:
        return await asyncio.to_thread(
            self._create_session_sync,
            session_id,
            agent,
            backend=backend,
            model=model,
            source=source,
            parent_session_id=parent_session_id,
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
        metadata: Mapping[str, Any] | None = None,
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
        parent_session_id: str | None = None,
    ) -> list[SessionRecord]:
        return await asyncio.to_thread(
            self._list_sessions_sync,
            status,
            backend,
            source,
            parent_session_id,
        )

    async def reap_orphaned_sessions(self) -> int:
        return await asyncio.to_thread(self._reap_orphaned_sessions_sync)

    def close(self) -> None:
        """Close the thread-local connection."""
        if hasattr(self._local, "conn") and self._local.conn is not None:
            self._local.conn.close()
            self._local.conn = None
