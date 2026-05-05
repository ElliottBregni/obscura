"""PostgreSQL adapter for Obscura event store - API compatible with SQLite."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from obscura.core.enums.agent import AgentEventKind
from obscura.core.enums.lifecycle import (
    SESSION_VALID_TRANSITIONS as VALID_TRANSITIONS,
)
from obscura.core.enums.lifecycle import SessionStatus
from obscura.core.event_store import EventRecord, SessionRecord
from obscura.core.session_utils import list_active_sessions

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from obscura.core.types import AgentEvent

_psycopg2: Any
_RealDictCursor: Any
try:
    import psycopg2
    import psycopg2.pool  # noqa: F401  # pyright: ignore[reportUnusedImport]  needed for side-effect load
    from psycopg2.extras import RealDictCursor

    _has_psycopg2 = True
    _psycopg2 = psycopg2
    _RealDictCursor = RealDictCursor
except ImportError:
    logger.debug("suppressed exception in <module>", exc_info=True)
    _has_psycopg2 = False
    _psycopg2 = None
    _RealDictCursor = None

# Public re-export for callers checking availability before construction.
HAS_PSYCOPG2 = _has_psycopg2


def _coerce_metadata(value: object) -> dict[str, Any]:
    """Postgres returns JSONB as already-decoded ``dict``; legacy text rows
    arrive as JSON strings. Normalise either to a ``dict`` for SessionRecord.
    """
    if isinstance(value, dict):
        return cast(dict[str, Any], value)
    if isinstance(value, str) and value:
        try:
            decoded: object = json.loads(value)
        except json.JSONDecodeError:
            logger.debug("suppressed exception in _coerce_metadata", exc_info=True)
            return {}
        return cast(dict[str, Any], decoded) if isinstance(decoded, dict) else {}
    return {}


def _coerce_payload(value: object) -> dict[str, Any]:
    """JSONB or JSON-string payload column → ``dict[str, Any]``."""
    if isinstance(value, dict):
        return cast(dict[str, Any], value)
    if isinstance(value, str) and value:
        try:
            decoded: object = json.loads(value)
        except json.JSONDecodeError:
            logger.debug("suppressed exception in _coerce_payload", exc_info=True)
            return {}
        return cast(dict[str, Any], decoded) if isinstance(decoded, dict) else {}
    return {}


def _row_to_session(row: Mapping[str, Any]) -> SessionRecord:
    """Wrap a Postgres row dict into a SessionRecord via the standard parser.

    Postgres returns ``metadata`` as a decoded JSONB ``dict`` and timestamps
    as ``datetime`` objects, both of which ``SessionRecord.from_row`` already
    handles. Decoded metadata is re-injected so the row matches the
    `from_row` contract that expects either a JSON string or a dict.
    """
    return SessionRecord.from_row(
        {
            **dict(row),
            "metadata": _coerce_metadata(row.get("metadata")),
        }
    )


class PostgreSQLEventStore:
    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        database: str | None = None,
        user: str | None = None,
        password: str | None = None,
        min_connections: int = 2,
        max_connections: int = 10,
    ) -> None:
        if not HAS_PSYCOPG2:
            msg = "pip install psycopg2-binary"
            raise ImportError(msg)
        self.host: str = host or os.getenv("OBSCURA_DB_HOST", "localhost")
        self.port: int = port or int(os.getenv("OBSCURA_DB_PORT", "5432"))
        self.database: str = database or os.getenv("OBSCURA_DB_NAME", "obscura")
        self.user: str = user or os.getenv("OBSCURA_DB_USER", "obscura_user")
        self.password: str = password or os.getenv("OBSCURA_DB_PASSWORD", "")
        self._pool: Any = _psycopg2.pool.ThreadedConnectionPool(
            min_connections,
            max_connections,
            host=self.host,
            port=self.port,
            database=self.database,
            user=self.user,
            password=self.password,
            cursor_factory=_RealDictCursor,
        )
        self._init_schema()

    def _get_conn(self) -> Any:
        return self._pool.getconn()

    def _put_conn(self, conn: Any) -> None:
        self._pool.putconn(conn)

    def _init_schema(self) -> None:
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("CREATE SCHEMA IF NOT EXISTS events")
                cur.execute(
                    """CREATE TABLE IF NOT EXISTS events.sessions (id TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'running', active_agent TEXT NOT NULL DEFAULT '', created_at TIMESTAMP WITH TIME ZONE NOT NULL, updated_at TIMESTAMP WITH TIME ZONE NOT NULL, backend TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT 'live', project TEXT NOT NULL DEFAULT '', summary TEXT NOT NULL DEFAULT '', message_count INTEGER NOT NULL DEFAULT 0, metadata JSONB NOT NULL DEFAULT '{}'::jsonb)""",
                )
                cur.execute(
                    """CREATE TABLE IF NOT EXISTS events.events (session_id TEXT NOT NULL, seq INTEGER NOT NULL, kind TEXT NOT NULL, payload JSONB NOT NULL, timestamp TIMESTAMP WITH TIME ZONE NOT NULL, PRIMARY KEY (session_id, seq), FOREIGN KEY (session_id) REFERENCES events.sessions(id) ON DELETE CASCADE)""",
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_events_session ON events.events(session_id, seq)",
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_sessions_status ON events.sessions(status)",
                )
                conn.commit()
        finally:
            self._put_conn(conn)

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
            backend,
            model,
            source,
            parent_session_id,
            project,
            summary,
            metadata,
        )

    def _create_session_sync(
        self,
        session_id: str,
        agent: str,
        backend: str,
        model: str,
        source: str,
        parent_session_id: str,
        project: str,
        summary: str,
        metadata: Mapping[str, Any] | None,
    ) -> SessionRecord:
        now = datetime.now(UTC)
        meta = dict(metadata) if metadata else {}
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO events.sessions (id, status, backend, model, active_agent, source, parent_session_id, project, summary, message_count, metadata, created_at, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 0, %s, %s, %s)",
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
                        json.dumps(meta),
                        now,
                        now,
                    ),
                )
                conn.commit()
        finally:
            self._put_conn(conn)
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
            metadata=meta,
            created_at=now,
            updated_at=now,
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

    async def reap_orphaned_sessions(self) -> int:
        return await asyncio.to_thread(self._reap_orphaned_sessions_sync)

    async def append(self, session_id: str, event: AgentEvent) -> EventRecord:
        return await asyncio.to_thread(self._append_sync, session_id, event)

    async def get_events(
        self, session_id: str, *, after_seq: int = 0
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

    def _get_session_sync(self, session_id: str) -> SessionRecord | None:
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM events.sessions WHERE id = %s",
                    (session_id,),
                )
                row_any: Any = cur.fetchone()
                if not row_any:
                    return None
                return _row_to_session(cast(Mapping[str, Any], row_any))
        finally:
            self._put_conn(conn)

    def _update_status_sync(
        self,
        session_id: str,
        status: SessionStatus,
    ) -> None:
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT status FROM events.sessions WHERE id = %s",
                    (session_id,),
                )
                row_any: Any = cur.fetchone()
                if not row_any:
                    msg = f"Session not found: {session_id}"
                    raise ValueError(msg)
                row = cast(Mapping[str, Any], row_any)
                current = SessionStatus(row["status"])
                if status not in VALID_TRANSITIONS[current]:
                    msg = f"Invalid transition: {current.value} -> {status.value}"
                    raise ValueError(msg)
                now = datetime.now(UTC)
                cur.execute(
                    "UPDATE events.sessions SET status = %s, updated_at = %s WHERE id = %s",
                    (status.value, now, session_id),
                )
                conn.commit()
        finally:
            self._put_conn(conn)

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
            sets.append("metadata = %s")
            params.append(json.dumps(dict(metadata), default=str))

        if not sets:
            return

        sets.append("updated_at = %s")
        params.append(datetime.now(UTC))
        params.append(session_id)

        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE events.sessions SET {', '.join(sets)} WHERE id = %s",
                    params,
                )
                conn.commit()
        finally:
            self._put_conn(conn)

    def _reap_orphaned_sessions_sync(self) -> int:
        active_statuses = (
            SessionStatus.RUNNING,
            SessionStatus.WAITING_FOR_TOOL,
            SessionStatus.WAITING_FOR_USER,
        )
        placeholders = ",".join(["%s"] * len(active_statuses))
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT id FROM events.sessions WHERE status IN ({placeholders})",
                    [s.value for s in active_statuses],
                )
                rows = cast(list[Mapping[str, Any]], cur.fetchall())
                if not rows:
                    return 0

                alive_ids = {
                    s.get("session_id", "")[:16] for s in list_active_sessions()
                }
                reaped = 0
                now = datetime.now(UTC)
                for row in rows:
                    sid = str(row["id"])
                    if sid[:16] not in alive_ids:
                        cur.execute(
                            "UPDATE events.sessions SET status = %s, updated_at = %s WHERE id = %s",
                            (SessionStatus.FAILED.value, now, sid),
                        )
                        reaped += 1
                if reaped:
                    conn.commit()
                return reaped
        finally:
            self._put_conn(conn)

    def _append_sync(self, session_id: str, event: AgentEvent) -> EventRecord:
        now = datetime.now(UTC)
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
        conn = self._get_conn()
        seq: int = 0
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COALESCE(MAX(seq), 0) + 1 FROM events.events WHERE session_id = %s",
                    (session_id,),
                )
                seq_row = cast(Mapping[str, Any], cur.fetchone())
                seq = int(seq_row["coalesce"])
                cur.execute(
                    "INSERT INTO events.events (session_id, seq, kind, payload, timestamp) VALUES (%s, %s, %s, %s, %s)",
                    (session_id, seq, event.kind.value, json.dumps(payload), now),
                )
                cur.execute(
                    "UPDATE events.sessions SET updated_at = %s WHERE id = %s",
                    (now, session_id),
                )
                conn.commit()
        finally:
            self._put_conn(conn)
        return EventRecord(
            session_id=session_id,
            seq=seq,
            kind=event.kind,
            payload=payload,
            timestamp=now,
        )

    def _get_events_sync(self, session_id: str, after_seq: int) -> list[EventRecord]:
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM events.events WHERE session_id = %s AND seq > %s ORDER BY seq",
                    (session_id, after_seq),
                )
                rows = cast(list[Mapping[str, Any]], cur.fetchall())
                results: list[EventRecord] = []
                for r in rows:
                    payload = _coerce_payload(r["payload"])
                    results.append(
                        EventRecord(
                            session_id=str(r["session_id"]),
                            seq=int(r["seq"]),
                            kind=AgentEventKind(r["kind"]),
                            payload=payload,
                            timestamp=r["timestamp"],
                        )
                    )
                return results
        finally:
            self._put_conn(conn)

    def _list_sessions_sync(
        self,
        status: SessionStatus | None,
        backend: str | None,
        source: str | None,
        parent_session_id: str | None = None,
    ) -> list[SessionRecord]:
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                query = "SELECT * FROM events.sessions WHERE 1=1"
                params: list[Any] = []
                if status:
                    query += " AND status = %s"
                    params.append(status.value)
                if backend:
                    query += " AND backend = %s"
                    params.append(backend)
                if source:
                    query += " AND source = %s"
                    params.append(source)
                if parent_session_id is not None:
                    query += " AND parent_session_id = %s"
                    params.append(parent_session_id)
                query += " ORDER BY updated_at DESC"
                cur.execute(query, params)
                rows = cast(list[Mapping[str, Any]], cur.fetchall())
                return [_row_to_session(r) for r in rows]
        finally:
            self._put_conn(conn)

    def close(self) -> None:
        if hasattr(self, "_pool"):
            self._pool.closeall()
