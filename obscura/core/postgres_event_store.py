"""PostgreSQL adapter for Obscura event store - API compatible with SQLite."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from obscura.core.event_store import EventRecord, SessionRecord, SessionStatus
from obscura.core.enums.agent import AgentEventKind

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
        metadata: dict[str, Any] | None = None,
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
        metadata: dict[str, Any] | None,
    ) -> SessionRecord:
        now = datetime.now(UTC)
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
                        json.dumps(metadata or {}),
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
            metadata=metadata or {},
            created_at=now,
            updated_at=now,
        )

    async def get_session(self, session_id: str) -> SessionRecord | None:
        return await asyncio.to_thread(self._get_session_sync, session_id)

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
                row = cast(dict[str, Any], row_any)
                meta_raw: Any = row["metadata"]
                meta: dict[str, Any] = (
                    cast(dict[str, Any], meta_raw)
                    if isinstance(meta_raw, dict)
                    else json.loads(meta_raw or "{}")
                )
                return SessionRecord(
                    id=str(row["id"]),
                    status=SessionStatus(row["status"]),
                    backend=str(row["backend"]),
                    model=str(row["model"]),
                    active_agent=str(row["active_agent"]),
                    source=str(row["source"]),
                    project=str(row["project"]),
                    summary=str(row["summary"]),
                    message_count=int(row["message_count"]),
                    metadata=meta,
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                )
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
                seq_row = cast(dict[str, Any], cur.fetchone())
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
                rows = cast(list[dict[str, Any]], cur.fetchall())
                results: list[EventRecord] = []
                for r in rows:
                    payload_raw: Any = r["payload"]
                    payload: dict[str, Any] = (
                        cast(dict[str, Any], payload_raw)
                        if isinstance(payload_raw, dict)
                        else json.loads(payload_raw)
                    )
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
                rows = cast(list[dict[str, Any]], cur.fetchall())
                results: list[SessionRecord] = []
                for r in rows:
                    meta_raw: Any = r["metadata"]
                    meta: dict[str, Any] = (
                        cast(dict[str, Any], meta_raw)
                        if isinstance(meta_raw, dict)
                        else json.loads(meta_raw or "{}")
                    )
                    results.append(
                        SessionRecord(
                            id=str(r["id"]),
                            status=SessionStatus(r["status"]),
                            backend=str(r["backend"]),
                            model=str(r["model"]),
                            active_agent=str(r["active_agent"]),
                            source=str(r["source"]),
                            parent_session_id=str(r.get("parent_session_id", "") or ""),
                            project=str(r["project"]),
                            summary=str(r["summary"]),
                            message_count=int(r["message_count"]),
                            metadata=meta,
                            created_at=r["created_at"],
                            updated_at=r["updated_at"],
                        )
                    )
                return results
        finally:
            self._put_conn(conn)

    def close(self) -> None:
        if hasattr(self, "_pool"):
            self._pool.closeall()
