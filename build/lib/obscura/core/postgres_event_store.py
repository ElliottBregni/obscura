"""PostgreSQL adapter for Obscura event store - API compatible with SQLite."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from typing import Any

try:
    import psycopg2
    import psycopg2.pool
    from psycopg2.extras import RealDictCursor

    HAS_PSYCOPG2 = True
except ImportError:
    HAS_PSYCOPG2 = False  # pyright: ignore[reportConstantRedefinition]

from obscura.core.event_store import (
    EventRecord,
    SessionRecord,
    SessionStatus,
    SnapshotRecord,
)
from obscura.core.types import AgentEvent, AgentEventKind


class PostgreSQLEventStore:
    def __init__(
        self,
        host=None,
        port=None,
        database=None,
        user=None,
        password=None,
        min_connections=2,
        max_connections=10,
    ) -> None:
        if not HAS_PSYCOPG2:
            msg = "pip install psycopg2-binary"
            raise ImportError(msg)
        self.host = host or os.getenv("OBSCURA_DB_HOST", "localhost")
        self.port = port or int(os.getenv("OBSCURA_DB_PORT", "5432"))
        self.database = database or os.getenv("OBSCURA_DB_NAME", "obscura")
        self.user = user or os.getenv("OBSCURA_DB_USER", "obscura_user")
        self.password = password or os.getenv("OBSCURA_DB_PASSWORD", "")
        self._pool = psycopg2.pool.ThreadedConnectionPool(
            min_connections,
            max_connections,
            host=self.host,
            port=self.port,
            database=self.database,
            user=self.user,
            password=self.password,
            cursor_factory=RealDictCursor,
        )
        self._init_schema()

    def _get_conn(self):  # type: ignore[no-untyped-def]
        return self._pool.getconn()

    def _put_conn(self, conn) -> None:  # type: ignore[no-untyped-def]
        self._pool.putconn(conn)

    def _init_schema(self) -> None:
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("CREATE SCHEMA IF NOT EXISTS events")
                cur.execute(
                    """CREATE TABLE IF NOT EXISTS events.sessions (id TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'running', active_agent TEXT NOT NULL DEFAULT '', created_at TIMESTAMP WITH TIME ZONE NOT NULL, updated_at TIMESTAMP WITH TIME ZONE NOT NULL, backend TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT 'live', parent_session_id TEXT NOT NULL DEFAULT '', project TEXT NOT NULL DEFAULT '', summary TEXT NOT NULL DEFAULT '', message_count INTEGER NOT NULL DEFAULT 0, metadata JSONB NOT NULL DEFAULT '{}'::jsonb, branched_at_seq INTEGER NOT NULL DEFAULT 0, root_session_id TEXT NOT NULL DEFAULT '', frozen BOOLEAN NOT NULL DEFAULT FALSE)""",
                )
                # Idempotent migrations for upgrades from older schemas.
                for col_def in (
                    "ADD COLUMN IF NOT EXISTS parent_session_id TEXT NOT NULL DEFAULT ''",
                    "ADD COLUMN IF NOT EXISTS branched_at_seq INTEGER NOT NULL DEFAULT 0",
                    "ADD COLUMN IF NOT EXISTS root_session_id TEXT NOT NULL DEFAULT ''",
                    "ADD COLUMN IF NOT EXISTS frozen BOOLEAN NOT NULL DEFAULT FALSE",
                ):
                    cur.execute(f"ALTER TABLE events.sessions {col_def}")
                cur.execute(
                    """CREATE TABLE IF NOT EXISTS events.events (session_id TEXT NOT NULL, seq INTEGER NOT NULL, kind TEXT NOT NULL, payload JSONB NOT NULL, timestamp TIMESTAMP WITH TIME ZONE NOT NULL, PRIMARY KEY (session_id, seq), FOREIGN KEY (session_id) REFERENCES events.sessions(id) ON DELETE CASCADE)""",
                )
                cur.execute(
                    """CREATE TABLE IF NOT EXISTS events.session_snapshots (session_id TEXT NOT NULL, up_to_seq INTEGER NOT NULL, context_blob TEXT NOT NULL, format_version INTEGER NOT NULL DEFAULT 1, created_at TIMESTAMP WITH TIME ZONE NOT NULL, PRIMARY KEY (session_id, up_to_seq), FOREIGN KEY (session_id) REFERENCES events.sessions(id) ON DELETE CASCADE)""",
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_events_session ON events.events(session_id, seq)",
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_sessions_status ON events.sessions(status)",
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_sessions_root ON events.sessions(root_session_id)",
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_snapshots_session ON events.session_snapshots(session_id, up_to_seq DESC)",
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
        root_session_id = "" if parent_session_id else session_id
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO events.sessions (id, status, backend, model, active_agent, source, parent_session_id, project, summary, message_count, metadata, created_at, updated_at, branched_at_seq, root_session_id, frozen) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 0, %s, %s, %s, 0, %s, FALSE)",
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
                        root_session_id,
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
            branched_at_seq=0,
            root_session_id=root_session_id,
            frozen=False,
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

    def _row_to_session(self, row: dict[str, Any]) -> SessionRecord:
        meta = (
            row["metadata"]
            if isinstance(row["metadata"], dict)
            else json.loads(row["metadata"] or "{}")
        )
        return SessionRecord(
            id=row["id"],
            status=SessionStatus(row["status"]),
            backend=row.get("backend", "") or "",
            model=row.get("model", "") or "",
            active_agent=row.get("active_agent", "") or "",
            source=row.get("source", "live") or "live",
            parent_session_id=row.get("parent_session_id", "") or "",
            project=row.get("project", "") or "",
            summary=row.get("summary", "") or "",
            message_count=int(row.get("message_count") or 0),
            metadata=meta,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            branched_at_seq=int(row.get("branched_at_seq") or 0),
            root_session_id=row.get("root_session_id", "") or "",
            frozen=bool(row.get("frozen", False)),
        )

    def _get_session_sync(self, session_id: str) -> SessionRecord | None:
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM events.sessions WHERE id = %s",
                    (session_id,),
                )
                row = cur.fetchone()
                if not row:
                    return None
                return self._row_to_session(row)
        finally:
            self._put_conn(conn)

    def _append_sync(self, session_id: str, event: AgentEvent) -> EventRecord:
        now = datetime.now(UTC)
        payload = {
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
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT frozen FROM events.sessions WHERE id = %s",
                    (session_id,),
                )
                frozen_row = cur.fetchone()
                if frozen_row is not None and bool(frozen_row.get("frozen", False)):
                    msg = "session frozen"
                    raise ValueError(msg)

                cur.execute(
                    "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM events.events WHERE session_id = %s",
                    (session_id,),
                )
                seq = cur.fetchone()["next_seq"]
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
                return [
                    EventRecord(
                        session_id=r["session_id"],
                        seq=r["seq"],
                        kind=AgentEventKind(r["kind"]),
                        payload=r["payload"]
                        if isinstance(r["payload"], dict)
                        else json.loads(r["payload"]),
                        timestamp=r["timestamp"],
                    )
                    for r in cur.fetchall()
                ]
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
                return [self._row_to_session(r) for r in cur.fetchall()]
        finally:
            self._put_conn(conn)

    # -- branching primitives ------------------------------------------------

    async def fork(
        self,
        parent_session_id: str,
        at_seq: int,
        *,
        new_session_id: str,
        agent: str,
        backend: str = "",
        model: str = "",
        summary: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> SessionRecord:
        return await asyncio.to_thread(
            self._fork_sync,
            parent_session_id,
            at_seq,
            new_session_id,
            agent,
            backend,
            model,
            summary,
            metadata,
        )

    def _fork_sync(
        self,
        parent_session_id: str,
        at_seq: int,
        new_session_id: str,
        agent: str,
        backend: str,
        model: str,
        summary: str,
        metadata: dict[str, Any] | None,
    ) -> SessionRecord:
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM events.sessions WHERE id = %s FOR UPDATE",
                    (parent_session_id,),
                )
                parent_row = cur.fetchone()
                if parent_row is None:
                    msg = "parent not found"
                    raise ValueError(msg)
                parent = self._row_to_session(parent_row)

                cur.execute(
                    "SELECT COALESCE(MAX(seq), 0) AS max_seq FROM events.events WHERE session_id = %s",
                    (parent_session_id,),
                )
                parent_max_seq = int(cur.fetchone()["max_seq"])
                if at_seq < 0 or at_seq > parent_max_seq:
                    msg = "at_seq out of range"
                    raise ValueError(msg)

                root = parent.root_session_id or parent.id
                child_backend = backend or parent.backend
                child_model = model or parent.model
                now = datetime.now(UTC)

                cur.execute(
                    "INSERT INTO events.sessions (id, status, backend, model, active_agent, source, parent_session_id, project, summary, message_count, metadata, created_at, updated_at, branched_at_seq, root_session_id, frozen) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 0, %s, %s, %s, %s, %s, FALSE)",
                    (
                        new_session_id,
                        SessionStatus.RUNNING.value,
                        child_backend,
                        child_model,
                        agent,
                        "live",
                        parent_session_id,
                        parent.project,
                        summary,
                        json.dumps(metadata or {}),
                        now,
                        now,
                        at_seq,
                        root,
                    ),
                )
                cur.execute(
                    "UPDATE events.sessions SET frozen = TRUE, updated_at = %s WHERE id = %s",
                    (now, parent_session_id),
                )
                conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._put_conn(conn)

        return SessionRecord(
            id=new_session_id,
            status=SessionStatus.RUNNING,
            backend=child_backend,
            model=child_model,
            active_agent=agent,
            source="live",
            parent_session_id=parent_session_id,
            project=parent.project,
            summary=summary,
            message_count=0,
            metadata=metadata or {},
            created_at=now,
            updated_at=now,
            branched_at_seq=at_seq,
            root_session_id=root,
            frozen=False,
        )

    async def freeze_session(self, session_id: str) -> None:
        await asyncio.to_thread(self._freeze_session_sync, session_id)

    def _freeze_session_sync(self, session_id: str) -> None:
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id FROM events.sessions WHERE id = %s",
                    (session_id,),
                )
                if cur.fetchone() is None:
                    msg = f"Session not found: {session_id}"
                    raise ValueError(msg)
                now = datetime.now(UTC)
                cur.execute(
                    "UPDATE events.sessions SET frozen = TRUE, updated_at = %s WHERE id = %s",
                    (now, session_id),
                )
                conn.commit()
        finally:
            self._put_conn(conn)

    def _walk_chain_sync(self, session_id: str) -> list[tuple[str, int | None]]:
        conn = self._get_conn()
        try:
            chain: list[tuple[str, int | None]] = []
            sid: str | None = session_id
            upper: int | None = None
            with conn.cursor() as cur:
                while sid:
                    cur.execute(
                        "SELECT parent_session_id, branched_at_seq FROM events.sessions WHERE id = %s",
                        (sid,),
                    )
                    row = cur.fetchone()
                    if row is None:
                        break
                    chain.append((sid, upper))
                    parent_id = row.get("parent_session_id", "") or ""
                    if not parent_id:
                        break
                    upper = int(row.get("branched_at_seq") or 0)
                    sid = parent_id
            chain.reverse()
            return chain
        finally:
            self._put_conn(conn)

    async def materialize_events(self, session_id: str) -> list[EventRecord]:
        return await asyncio.to_thread(self._materialize_events_sync, session_id)

    def _materialize_events_sync(self, session_id: str) -> list[EventRecord]:
        chain = self._walk_chain_sync(session_id)
        out: list[EventRecord] = []
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                for sid, upper in chain:
                    if upper is None:
                        cur.execute(
                            "SELECT * FROM events.events WHERE session_id = %s ORDER BY seq",
                            (sid,),
                        )
                    else:
                        cur.execute(
                            "SELECT * FROM events.events WHERE session_id = %s AND seq <= %s ORDER BY seq",
                            (sid, upper),
                        )
                    for r in cur.fetchall():
                        out.append(
                            EventRecord(
                                session_id=r["session_id"],
                                seq=r["seq"],
                                kind=AgentEventKind(r["kind"]),
                                payload=r["payload"]
                                if isinstance(r["payload"], dict)
                                else json.loads(r["payload"]),
                                timestamp=r["timestamp"],
                            ),
                        )
            return out
        finally:
            self._put_conn(conn)

    async def write_snapshot(
        self,
        session_id: str,
        up_to_seq: int,
        context_blob: str,
        format_version: int = 1,
    ) -> SnapshotRecord:
        return await asyncio.to_thread(
            self._write_snapshot_sync,
            session_id,
            up_to_seq,
            context_blob,
            format_version,
        )

    def _write_snapshot_sync(
        self,
        session_id: str,
        up_to_seq: int,
        context_blob: str,
        format_version: int,
    ) -> SnapshotRecord:
        now = datetime.now(UTC)
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO events.session_snapshots (session_id, up_to_seq, context_blob, format_version, created_at) "
                    "VALUES (%s, %s, %s, %s, %s) "
                    "ON CONFLICT (session_id, up_to_seq) DO UPDATE SET "
                    "context_blob = EXCLUDED.context_blob, "
                    "format_version = EXCLUDED.format_version, "
                    "created_at = EXCLUDED.created_at",
                    (session_id, up_to_seq, context_blob, format_version, now),
                )
                conn.commit()
        finally:
            self._put_conn(conn)
        return SnapshotRecord(
            session_id=session_id,
            up_to_seq=up_to_seq,
            context_blob=context_blob,
            format_version=format_version,
            created_at=now,
        )

    async def get_nearest_snapshot(
        self,
        session_id: str,
        max_seq: int | None = None,
    ) -> SnapshotRecord | None:
        return await asyncio.to_thread(
            self._get_nearest_snapshot_sync,
            session_id,
            max_seq,
        )

    def _get_nearest_snapshot_sync(
        self,
        session_id: str,
        max_seq: int | None,
    ) -> SnapshotRecord | None:
        chain = self._walk_chain_sync(session_id)
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                for sid, upper in reversed(chain):
                    bound = max_seq if sid == session_id else upper
                    if bound is None:
                        cur.execute(
                            "SELECT * FROM events.session_snapshots WHERE session_id = %s "
                            "ORDER BY up_to_seq DESC LIMIT 1",
                            (sid,),
                        )
                    else:
                        cur.execute(
                            "SELECT * FROM events.session_snapshots WHERE session_id = %s AND up_to_seq <= %s "
                            "ORDER BY up_to_seq DESC LIMIT 1",
                            (sid, bound),
                        )
                    row = cur.fetchone()
                    if row is not None:
                        return SnapshotRecord(
                            session_id=row["session_id"],
                            up_to_seq=int(row["up_to_seq"]),
                            context_blob=row["context_blob"],
                            format_version=int(row["format_version"]),
                            created_at=row["created_at"],
                        )
            return None
        finally:
            self._put_conn(conn)

    async def list_snapshots(self, session_id: str) -> list[SnapshotRecord]:
        return await asyncio.to_thread(self._list_snapshots_sync, session_id)

    def _list_snapshots_sync(self, session_id: str) -> list[SnapshotRecord]:
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM events.session_snapshots WHERE session_id = %s ORDER BY up_to_seq",
                    (session_id,),
                )
                return [
                    SnapshotRecord(
                        session_id=r["session_id"],
                        up_to_seq=int(r["up_to_seq"]),
                        context_blob=r["context_blob"],
                        format_version=int(r["format_version"]),
                        created_at=r["created_at"],
                    )
                    for r in cur.fetchall()
                ]
        finally:
            self._put_conn(conn)

    async def materialize_prefix_into_child(self, child_session_id: str) -> int:
        return await asyncio.to_thread(
            self._materialize_prefix_into_child_sync,
            child_session_id,
        )

    def _materialize_prefix_into_child_sync(self, child_session_id: str) -> int:
        child = self._get_session_sync(child_session_id)
        if child is None:
            msg = f"Session not found: {child_session_id}"
            raise ValueError(msg)
        if not child.parent_session_id:
            return 0

        parent = self._get_session_sync(child.parent_session_id)
        branched_at = child.branched_at_seq
        if parent is not None and parent.parent_session_id:
            inlined = self._materialize_prefix_into_child_sync(child.parent_session_id)
            branched_at += inlined

        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT seq, kind, payload, timestamp FROM events.events "
                    "WHERE session_id = %s AND seq <= %s ORDER BY seq",
                    (child.parent_session_id, branched_at),
                )
                parent_events = cur.fetchall()
                n = len(parent_events)

                if n > 0:
                    cur.execute(
                        "SELECT seq FROM events.events WHERE session_id = %s ORDER BY seq DESC",
                        (child_session_id,),
                    )
                    for row in cur.fetchall():
                        cur.execute(
                            "UPDATE events.events SET seq = %s WHERE session_id = %s AND seq = %s",
                            (int(row["seq"]) + n, child_session_id, int(row["seq"])),
                        )

                    for row in parent_events:
                        payload = (
                            row["payload"]
                            if isinstance(row["payload"], dict)
                            else json.loads(row["payload"])
                        )
                        cur.execute(
                            "INSERT INTO events.events (session_id, seq, kind, payload, timestamp) "
                            "VALUES (%s, %s, %s, %s, %s)",
                            (
                                child_session_id,
                                int(row["seq"]),
                                row["kind"],
                                json.dumps(payload),
                                row["timestamp"],
                            ),
                        )

                now = datetime.now(UTC)
                cur.execute(
                    "UPDATE events.sessions SET parent_session_id = '', branched_at_seq = 0, updated_at = %s WHERE id = %s",
                    (now, child_session_id),
                )
                conn.commit()
                return n
        except Exception:
            conn.rollback()
            raise
        finally:
            self._put_conn(conn)

    def close(self) -> None:
        if hasattr(self, "_pool"):
            self._pool.closeall()
