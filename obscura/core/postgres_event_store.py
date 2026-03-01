"""PostgreSQL adapter for Obscura event store - API compatible with SQLite."""
from __future__ import annotations
import asyncio, json, os
from datetime import UTC, datetime
from typing import Any

try:
    import psycopg2, psycopg2.pool
    from psycopg2.extras import RealDictCursor
    HAS_PSYCOPG2 = True
except ImportError:
    HAS_PSYCOPG2 = False

from obscura.core.event_store import EventRecord, SessionRecord, SessionStatus
from obscura.core.types import AgentEvent, AgentEventKind

class PostgreSQLEventStore:
    def __init__(self, host=None, port=None, database=None, user=None, password=None, min_connections=2, max_connections=10):
        if not HAS_PSYCOPG2:
            raise ImportError("pip install psycopg2-binary")
        self.host = host or os.getenv("OBSCURA_DB_HOST", "localhost")
        self.port = port or int(os.getenv("OBSCURA_DB_PORT", "5432"))
        self.database = database or os.getenv("OBSCURA_DB_NAME", "obscura")
        self.user = user or os.getenv("OBSCURA_DB_USER", "obscura_user")
        self.password = password or os.getenv("OBSCURA_DB_PASSWORD", "")
        self._pool = psycopg2.pool.ThreadedConnectionPool(min_connections, max_connections, host=self.host, port=self.port, database=self.database, user=self.user, password=self.password, cursor_factory=RealDictCursor)
        self._init_schema()
    
    def _get_conn(self): return self._pool.getconn()
    def _put_conn(self, conn): self._pool.putconn(conn)
    
    def _init_schema(self):
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("CREATE SCHEMA IF NOT EXISTS events")
                cur.execute("""CREATE TABLE IF NOT EXISTS events.sessions (id TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'running', active_agent TEXT NOT NULL DEFAULT '', created_at TIMESTAMP WITH TIME ZONE NOT NULL, updated_at TIMESTAMP WITH TIME ZONE NOT NULL, backend TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT 'live', project TEXT NOT NULL DEFAULT '', summary TEXT NOT NULL DEFAULT '', message_count INTEGER NOT NULL DEFAULT 0, metadata JSONB NOT NULL DEFAULT '{}'::jsonb)""")
                cur.execute("""CREATE TABLE IF NOT EXISTS events.events (session_id TEXT NOT NULL, seq INTEGER NOT NULL, kind TEXT NOT NULL, payload JSONB NOT NULL, timestamp TIMESTAMP WITH TIME ZONE NOT NULL, PRIMARY KEY (session_id, seq), FOREIGN KEY (session_id) REFERENCES events.sessions(id) ON DELETE CASCADE)""")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_events_session ON events.events(session_id, seq)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_sessions_status ON events.sessions(status)")
                conn.commit()
        finally:
            self._put_conn(conn)
    
    async def create_session(self, session_id, agent, *, backend="", model="", source="live", project="", summary="", metadata=None):
        return await asyncio.to_thread(self._create_session_sync, session_id, agent, backend, model, source, project, summary, metadata)
    
    def _create_session_sync(self, session_id, agent, backend, model, source, project, summary, metadata):
        now = datetime.now(UTC)
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO events.sessions (id, status, backend, model, active_agent, source, project, summary, message_count, metadata, created_at, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 0, %s, %s, %s)", (session_id, SessionStatus.RUNNING.value, backend, model, agent, source, project, summary, json.dumps(metadata or {}), now, now))
                conn.commit()
        finally:
            self._put_conn(conn)
        return SessionRecord(id=session_id, status=SessionStatus.RUNNING, backend=backend, model=model, active_agent=agent, source=source, project=project, summary=summary, message_count=0, metadata=metadata or {}, created_at=now, updated_at=now)
    
    async def get_session(self, session_id): return await asyncio.to_thread(self._get_session_sync, session_id)
    async def append(self, session_id, event): return await asyncio.to_thread(self._append_sync, session_id, event)
    async def get_events(self, session_id, *, after_seq=0): return await asyncio.to_thread(self._get_events_sync, session_id, after_seq)
    async def list_sessions(self, *, status=None, backend=None, source=None): return await asyncio.to_thread(self._list_sessions_sync, status, backend, source)
    
    def _get_session_sync(self, session_id):
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM events.sessions WHERE id = %s", (session_id,))
                row = cur.fetchone()
                if not row: return None
                meta = row["metadata"] if isinstance(row["metadata"], dict) else json.loads(row["metadata"] or "{}")
                return SessionRecord(id=row["id"], status=SessionStatus(row["status"]), backend=row["backend"], model=row["model"], active_agent=row["active_agent"], source=row["source"], project=row["project"], summary=row["summary"], message_count=row["message_count"], metadata=meta, created_at=row["created_at"], updated_at=row["updated_at"])
        finally:
            self._put_conn(conn)
    
    def _append_sync(self, session_id, event):
        now = datetime.now(UTC)
        payload = {"kind": event.kind.value, "text": event.text, "tool_name": event.tool_name, "tool_input": event.tool_input, "tool_result": event.tool_result, "tool_use_id": event.tool_use_id, "is_error": event.is_error, "turn": event.turn}
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM events.events WHERE session_id = %s", (session_id,))
                seq = cur.fetchone()["coalesce"]
                cur.execute("INSERT INTO events.events (session_id, seq, kind, payload, timestamp) VALUES (%s, %s, %s, %s, %s)", (session_id, seq, event.kind.value, json.dumps(payload), now))
                cur.execute("UPDATE events.sessions SET updated_at = %s WHERE id = %s", (now, session_id))
                conn.commit()
        finally:
            self._put_conn(conn)
        return EventRecord(session_id=session_id, seq=seq, kind=event.kind, payload=payload, timestamp=now)
    
    def _get_events_sync(self, session_id, after_seq):
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM events.events WHERE session_id = %s AND seq > %s ORDER BY seq", (session_id, after_seq))
                return [EventRecord(session_id=r["session_id"], seq=r["seq"], kind=AgentEventKind(r["kind"]), payload=r["payload"] if isinstance(r["payload"], dict) else json.loads(r["payload"]), timestamp=r["timestamp"]) for r in cur.fetchall()]
        finally:
            self._put_conn(conn)
    
    def _list_sessions_sync(self, status, backend, source):
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                query, params = "SELECT * FROM events.sessions WHERE 1=1", []
                if status: query += " AND status = %s"; params.append(status.value)
                if backend: query += " AND backend = %s"; params.append(backend)
                if source: query += " AND source = %s"; params.append(source)
                query += " ORDER BY updated_at DESC"
                cur.execute(query, params)
                return [SessionRecord(id=r["id"], status=SessionStatus(r["status"]), backend=r["backend"], model=r["model"], active_agent=r["active_agent"], source=r["source"], project=r["project"], summary=r["summary"], message_count=r["message_count"], metadata=r["metadata"] if isinstance(r["metadata"], dict) else json.loads(r["metadata"] or "{}"), created_at=r["created_at"], updated_at=r["updated_at"]) for r in cur.fetchall()]
        finally:
            self._put_conn(conn)
    
    def close(self):
        if hasattr(self, "_pool"): self._pool.closeall()
