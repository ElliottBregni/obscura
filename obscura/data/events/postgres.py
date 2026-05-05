"""PostgreSQL implementation of the :class:`EventRepo` Protocol.

Mirrors the SQLite schema and query surface so the only meaningful
difference between backends is concurrency semantics: Postgres scales
to many workers, SQLite is a single-file local store. Connections come
from :func:`obscura.data.engine.postgres_connection` (which itself uses
:class:`obscura.core.pg_config.PGPoolManager`).

Async public API delegates to sync helpers via ``asyncio.to_thread``,
matching the SQLite repo so callers never see a difference.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from obscura.core.enums.agent import AgentEventKind
from obscura.core.enums.lifecycle import (
    SESSION_VALID_TRANSITIONS as VALID_TRANSITIONS,
)
from obscura.core.enums.lifecycle import SessionStatus
from obscura.core.session_utils import list_active_sessions
from obscura.core.types import AgentEvent
from obscura.data.engine import postgres_connection
from obscura.data.events.protocol import EventRecord, SessionRecord

logger = logging.getLogger(__name__)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS obscura_sessions (
    id                 TEXT PRIMARY KEY,
    status             TEXT NOT NULL DEFAULT 'running',
    backend            TEXT NOT NULL DEFAULT '',
    model              TEXT NOT NULL DEFAULT '',
    active_agent       TEXT NOT NULL DEFAULT '',
    source             TEXT NOT NULL DEFAULT 'live',
    parent_session_id  TEXT NOT NULL DEFAULT '',
    project            TEXT NOT NULL DEFAULT '',
    summary            TEXT NOT NULL DEFAULT '',
    message_count      INTEGER NOT NULL DEFAULT 0,
    metadata           JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_obscura_sessions_status
    ON obscura_sessions(status);
CREATE INDEX IF NOT EXISTS idx_obscura_sessions_backend
    ON obscura_sessions(backend);
CREATE INDEX IF NOT EXISTS idx_obscura_sessions_source
    ON obscura_sessions(source);
CREATE INDEX IF NOT EXISTS idx_obscura_sessions_updated
    ON obscura_sessions(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_obscura_sessions_parent
    ON obscura_sessions(parent_session_id);

CREATE TABLE IF NOT EXISTS obscura_events (
    session_id  TEXT    NOT NULL REFERENCES obscura_sessions(id),
    seq         INTEGER NOT NULL,
    kind        TEXT    NOT NULL,
    payload     JSONB   NOT NULL,
    timestamp   TEXT    NOT NULL,
    PRIMARY KEY (session_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_obscura_events_session
    ON obscura_events(session_id, seq);
"""


_SESSION_COLS = (
    "id, status, backend, model, active_agent, source, parent_session_id, "
    "project, summary, message_count, metadata, created_at, updated_at"
)


_QUERIES = {
    "create_session": (
        "INSERT INTO obscura_sessions "
        "(id, status, backend, model, active_agent, source, parent_session_id, "
        " project, summary, message_count, metadata, created_at, updated_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 0, %s::jsonb, %s, %s)"
    ),
    "get_session": (f"SELECT {_SESSION_COLS} FROM obscura_sessions WHERE id = %s"),
    "get_session_status": "SELECT status FROM obscura_sessions WHERE id = %s",
    "update_status": (
        "UPDATE obscura_sessions SET status = %s, updated_at = %s WHERE id = %s"
    ),
    "max_seq_for_session": (
        "SELECT COALESCE(MAX(seq), 0) AS max_seq FROM obscura_events"
        " WHERE session_id = %s"
    ),
    "insert_event": (
        "INSERT INTO obscura_events (session_id, seq, kind, payload, timestamp) "
        "VALUES (%s, %s, %s, %s::jsonb, %s)"
    ),
    "get_events_after": (
        "SELECT session_id, seq, kind, payload, timestamp FROM obscura_events "
        "WHERE session_id = %s AND seq > %s ORDER BY seq"
    ),
}


def _serialize_event(event: AgentEvent) -> str:
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


def _deserialize_payload(raw: Any) -> dict[str, Any]:  # noqa: ANN401  # JSONB returns native dict or text
    if isinstance(raw, dict):
        return {str(k): v for k, v in raw.items()}
    if isinstance(raw, str):
        try:
            loaded = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            logger.debug("invalid payload json: %r", raw, exc_info=True)
            return {}
        return loaded if isinstance(loaded, dict) else {}
    return {}


def _row_to_session(row: Any) -> SessionRecord:  # noqa: ANN401  # psycopg2 RealDictRow
    """Build SessionRecord from a Postgres row (dict-like)."""
    metadata = row["metadata"] if isinstance(row["metadata"], dict) else {}
    return SessionRecord(
        id=row["id"],
        status=SessionStatus(row["status"]),
        backend=row["backend"] or "",
        model=row["model"] or "",
        active_agent=row["active_agent"] or "",
        source=row["source"] or "live",
        parent_session_id=row["parent_session_id"] or "",
        project=row["project"] or "",
        summary=row["summary"] or "",
        message_count=row["message_count"] or 0,
        metadata=metadata,
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


class PostgresEventRepo:
    """Postgres implementation of :class:`EventRepo`."""

    _schema_initialized = False

    def __init__(self) -> None:
        if PostgresEventRepo._schema_initialized:
            return
        with postgres_connection() as conn:
            try:
                with conn.cursor() as cur:
                    cur.execute(_SCHEMA)
                conn.commit()
                PostgresEventRepo._schema_initialized = True
            except Exception:
                conn.rollback()
                raise

    # -- sync helpers --------------------------------------------------------

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
        with postgres_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
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
        with postgres_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(_QUERIES["get_session"], (session_id,))
                row = cur.fetchone()
        if row is None:
            return None
        return _row_to_session(row)

    def _update_status_sync(
        self,
        session_id: str,
        status: SessionStatus,
    ) -> None:
        with postgres_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(_QUERIES["get_session_status"], (session_id,))
                row = cur.fetchone()
                if row is None:
                    msg = f"Session not found: {session_id}"
                    raise ValueError(msg)
                current = SessionStatus(row["status"])
                if status not in VALID_TRANSITIONS[current]:
                    msg = f"Invalid transition: {current.value} -> {status.value}"
                    raise ValueError(msg)
                now = datetime.now(UTC).isoformat()
                cur.execute(
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
        sets: list[str] = []
        params: list[Any] = []
        if summary is not None:
            sets.append("summary = %s")
            params.append(summary)
        if message_count is not None:
            sets.append("message_count = %s")
            params.append(message_count)
        if metadata is not None:
            sets.append("metadata = %s::jsonb")
            params.append(json.dumps(dict(metadata), default=str))
        if not sets:
            return
        sets.append("updated_at = %s")
        params.append(datetime.now(UTC).isoformat())
        params.append(session_id)
        with postgres_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE obscura_sessions SET {', '.join(sets)} WHERE id = %s",
                    params,
                )
            conn.commit()

    def _append_sync(
        self,
        session_id: str,
        event: AgentEvent,
    ) -> EventRecord:
        # SERIALIZABLE isolation prevents two writers from picking the
        # same MAX(seq) — Postgres detects the conflict and aborts one
        # of the transactions, which we let psycopg2 surface to the
        # caller. Same effect as SQLite's BEGIN IMMEDIATE.
        with postgres_connection() as conn:
            try:
                with conn.cursor() as cur:
                    cur.execute("BEGIN ISOLATION LEVEL SERIALIZABLE")
                    cur.execute(
                        _QUERIES["max_seq_for_session"],
                        (session_id,),
                    )
                    row = cur.fetchone()
                    seq = (row["max_seq"] if row else 0) + 1
                    now = datetime.now(UTC).isoformat()
                    payload = _serialize_event(event)
                    cur.execute(
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
        with postgres_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    _QUERIES["get_events_after"],
                    (session_id, after_seq),
                )
                rows = cur.fetchall()
        return [
            EventRecord(
                session_id=r["session_id"],
                seq=r["seq"],
                kind=AgentEventKind(r["kind"]),
                payload=_deserialize_payload(r["payload"]),
                timestamp=datetime.fromisoformat(r["timestamp"]),
            )
            for r in rows
        ]

    def _list_sessions_sync(
        self,
        status: SessionStatus | None = None,
        backend: str | None = None,
        source: str | None = None,
        parent_session_id: str | None = None,
    ) -> list[SessionRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if status is not None:
            clauses.append("status = %s")
            params.append(status.value)
        if backend is not None:
            clauses.append("backend = %s")
            params.append(backend)
        if source is not None:
            clauses.append("source = %s")
            params.append(source)
        if parent_session_id is not None:
            clauses.append("parent_session_id = %s")
            params.append(parent_session_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with postgres_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {_SESSION_COLS} FROM obscura_sessions{where}"
                    " ORDER BY updated_at DESC",
                    params,
                )
                rows = cur.fetchall()
        return [_row_to_session(r) for r in rows]

    def _reap_orphaned_sessions_sync(self) -> int:
        active = (
            SessionStatus.RUNNING,
            SessionStatus.WAITING_FOR_TOOL,
            SessionStatus.WAITING_FOR_USER,
        )
        placeholders = ",".join(["%s"] * len(active))
        with postgres_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT id FROM obscura_sessions WHERE status IN ({placeholders})",
                    [s.value for s in active],
                )
                rows = cur.fetchall()
                if not rows:
                    return 0
                alive_ids = {
                    s.get("session_id", "")[:16] for s in list_active_sessions()
                }
                reaped = 0
                now = datetime.now(UTC).isoformat()
                for row in rows:
                    sid = row["id"]
                    if sid[:16] not in alive_ids:
                        cur.execute(
                            _QUERIES["update_status"],
                            (SessionStatus.FAILED.value, now, sid),
                        )
                        reaped += 1
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
        """No-op: connections come from the shared pool."""
