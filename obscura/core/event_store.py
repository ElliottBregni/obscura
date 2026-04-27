"""obscura.core.event_store — Durable event-sourced session persistence.

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
from typing import Any, Protocol, cast, runtime_checkable

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
        },
    ),
    SessionStatus.WAITING_FOR_TOOL: frozenset(
        {
            SessionStatus.RUNNING,
            SessionStatus.PAUSED,
            SessionStatus.FAILED,
        },
    ),
    SessionStatus.WAITING_FOR_USER: frozenset(
        {
            SessionStatus.RUNNING,
            SessionStatus.PAUSED,
            SessionStatus.FAILED,
        },
    ),
    SessionStatus.PAUSED: frozenset(
        {
            SessionStatus.RUNNING,
            SessionStatus.FAILED,
        },
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
    parent_session_id: str = ""
    project: str = ""
    summary: str = ""
    message_count: int = 0
    metadata: dict[str, Any] = field(default_factory=_empty_dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    branched_at_seq: int = 0
    root_session_id: str = ""
    frozen: bool = False


@dataclass(frozen=True)
class EventRecord:
    """A single persisted event in the append-only log."""

    session_id: str
    seq: int
    kind: AgentEventKind
    payload: dict[str, Any]
    timestamp: datetime


@dataclass(frozen=True)
class SnapshotRecord:
    """A WAL-style checkpoint of materialized context up to a given seq."""

    session_id: str
    up_to_seq: int
    context_blob: str
    format_version: int
    created_at: datetime


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
        parent_session_id: str = "",
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
        parent_session_id: str | None = None,
    ) -> list[SessionRecord]: ...

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
    ) -> SessionRecord: ...

    async def freeze_session(self, session_id: str) -> None: ...

    async def materialize_events(self, session_id: str) -> list[EventRecord]: ...

    async def write_snapshot(
        self,
        session_id: str,
        up_to_seq: int,
        context_blob: str,
        format_version: int = 1,
    ) -> SnapshotRecord: ...

    async def get_nearest_snapshot(
        self,
        session_id: str,
        max_seq: int | None = None,
    ) -> SnapshotRecord | None: ...

    async def list_snapshots(self, session_id: str) -> list[SnapshotRecord]: ...

    async def materialize_prefix_into_child(self, child_session_id: str) -> int: ...


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
    if event.metadata is not None:
        from dataclasses import asdict

        payload["metadata"] = asdict(event.metadata)
    return json.dumps(payload, default=str)


def _deserialize_payload(raw: str) -> dict[str, Any]:
    """Deserialize a JSON payload string."""
    from typing import cast

    result: object = json.loads(raw)
    if not isinstance(result, dict):
        return {}
    return cast("dict[str, Any]", result)


_SESSION_COLS = (
    "id, status, backend, model, active_agent, source, parent_session_id, project, "
    "summary, message_count, metadata, created_at, updated_at, "
    "branched_at_seq, root_session_id, frozen"
)


def _row_to_session(row: sqlite3.Row) -> SessionRecord:
    """Convert a DB row to a SessionRecord."""
    raw_meta = row["metadata"]
    meta: dict[str, Any] = {}
    if raw_meta:
        try:
            parsed: Any = json.loads(raw_meta)
            if isinstance(parsed, dict):
                meta = cast("dict[str, Any]", parsed)
        except (json.JSONDecodeError, TypeError):
            pass
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
        message_count=int(row["message_count"] or 0),
        metadata=meta,
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        branched_at_seq=int(row["branched_at_seq"] or 0),
        root_session_id=row["root_session_id"] or "",
        frozen=bool(row["frozen"]),
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
            """,
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
            ("parent_session_id", "TEXT NOT NULL DEFAULT ''"),
            # user_id enables SOC2 C1 user-initiated deletion to cascade
            # from sessions to events. Existing rows default to '' and
            # are treated as orphaned (not deleted by delete_user_data).
            ("user_id", "TEXT NOT NULL DEFAULT ''"),
            ("branched_at_seq", "INTEGER NOT NULL DEFAULT 0"),
            ("root_session_id", "TEXT NOT NULL DEFAULT ''"),
            ("frozen", "INTEGER NOT NULL DEFAULT 0"),
        ]
        for col_name, col_def in _migrations:
            try:
                conn.execute(
                    f"ALTER TABLE sessions ADD COLUMN {col_name} {col_def}",
                )
            except sqlite3.OperationalError:
                pass  # column already exists

        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS session_snapshots (
                session_id      TEXT    NOT NULL,
                up_to_seq       INTEGER NOT NULL,
                context_blob    TEXT    NOT NULL,
                format_version  INTEGER NOT NULL DEFAULT 1,
                created_at      TEXT    NOT NULL,
                PRIMARY KEY (session_id, up_to_seq),
                FOREIGN KEY (session_id) REFERENCES sessions(id)
            );
            """,
        )

        # Add indexes for new columns
        for idx_sql in [
            "CREATE INDEX IF NOT EXISTS idx_sessions_backend ON sessions(backend)",
            "CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status)",
            "CREATE INDEX IF NOT EXISTS idx_sessions_source ON sessions(source)",
            "CREATE INDEX IF NOT EXISTS idx_sessions_updated ON sessions(updated_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_sessions_parent ON sessions(parent_session_id)",
            # Index user_id so deletion-by-user is not a full-table scan.
            "CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id) WHERE user_id != ''",
            "CREATE INDEX IF NOT EXISTS idx_sessions_root ON sessions(root_session_id)",
            "CREATE INDEX IF NOT EXISTS idx_snapshots_session ON session_snapshots(session_id, up_to_seq DESC)",
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
        parent_session_id: str = "",
        project: str = "",
        summary: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> SessionRecord:
        now = datetime.now(UTC).isoformat()
        meta_json = json.dumps(metadata or {}, default=str)
        root_session_id = "" if parent_session_id else session_id
        conn = self._conn()
        conn.execute(
            "INSERT INTO sessions "
            "(id, status, backend, model, active_agent, source, parent_session_id, project, "
            " summary, message_count, metadata, created_at, updated_at, "
            " branched_at_seq, root_session_id, frozen) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, 0, ?, 0)",
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
                root_session_id,
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
            metadata=metadata or {},
            created_at=datetime.fromisoformat(now),
            updated_at=datetime.fromisoformat(now),
            branched_at_seq=0,
            root_session_id=root_session_id,
            frozen=False,
        )

    def _get_session_sync(self, session_id: str) -> SessionRecord | None:
        row = (
            self._conn()
            .execute(
                f"SELECT {_SESSION_COLS} FROM sessions WHERE id = ?",
                (session_id,),
            )
            .fetchone()
        )
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
            msg = f"Session not found: {session_id}"
            raise ValueError(msg)

        current = SessionStatus(row["status"])
        if status not in VALID_TRANSITIONS[current]:
            msg = f"Invalid transition: {current.value} -> {status.value}"
            raise ValueError(
                msg,
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
            frozen_row = conn.execute(
                "SELECT frozen FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if frozen_row is not None and bool(frozen_row["frozen"]):
                msg = "session frozen"
                raise ValueError(msg)

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
        rows = (
            self._conn()
            .execute(
                "SELECT session_id, seq, kind, payload, timestamp "
                "FROM events WHERE session_id = ? AND seq > ? ORDER BY seq",
                (session_id, after_seq),
            )
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

    # -- branching primitives ------------------------------------------------

    def _fork_sync(
        self,
        parent_session_id: str,
        at_seq: int,
        *,
        new_session_id: str,
        agent: str,
        backend: str,
        model: str,
        summary: str,
        metadata: dict[str, Any] | None,
    ) -> SessionRecord:
        conn = self._conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            parent_row = conn.execute(
                f"SELECT {_SESSION_COLS} FROM sessions WHERE id = ?",
                (parent_session_id,),
            ).fetchone()
            if parent_row is None:
                msg = "parent not found"
                raise ValueError(msg)
            parent = _row_to_session(parent_row)

            max_row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS max_seq FROM events WHERE session_id = ?",
                (parent_session_id,),
            ).fetchone()
            parent_max_seq = int(max_row["max_seq"])
            if at_seq < 0 or at_seq > parent_max_seq:
                msg = "at_seq out of range"
                raise ValueError(msg)

            root = parent.root_session_id or parent.id
            child_backend = backend or parent.backend
            child_model = model or parent.model
            now = datetime.now(UTC).isoformat()
            meta_json = json.dumps(metadata or {}, default=str)

            conn.execute(
                "INSERT INTO sessions "
                "(id, status, backend, model, active_agent, source, parent_session_id, project, "
                " summary, message_count, metadata, created_at, updated_at, "
                " branched_at_seq, root_session_id, frozen) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, 0)",
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
                    meta_json,
                    now,
                    now,
                    at_seq,
                    root,
                ),
            )
            conn.execute(
                "UPDATE sessions SET frozen = 1, updated_at = ? WHERE id = ?",
                (now, parent_session_id),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

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
            created_at=datetime.fromisoformat(now),
            updated_at=datetime.fromisoformat(now),
            branched_at_seq=at_seq,
            root_session_id=root,
            frozen=False,
        )

    def _freeze_session_sync(self, session_id: str) -> None:
        conn = self._conn()
        row = conn.execute(
            "SELECT id FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            msg = f"Session not found: {session_id}"
            raise ValueError(msg)
        now = datetime.now(UTC).isoformat()
        conn.execute(
            "UPDATE sessions SET frozen = 1, updated_at = ? WHERE id = ?",
            (now, session_id),
        )
        conn.commit()

    def _walk_chain_sync(self, session_id: str) -> list[tuple[str, int | None]]:
        """Return [(sid, upper_bound_seq)] from root to leaf.

        For the leaf, upper_bound_seq is None (no upper bound). For each
        ancestor it is the branched_at_seq of its descendant in the chain.
        """
        conn = self._conn()
        chain: list[tuple[str, int | None]] = []
        sid: str | None = session_id
        upper: int | None = None
        while sid:
            row = conn.execute(
                "SELECT parent_session_id, branched_at_seq FROM sessions WHERE id = ?",
                (sid,),
            ).fetchone()
            if row is None:
                break
            chain.append((sid, upper))
            parent_id = row["parent_session_id"] or ""
            if not parent_id:
                break
            upper = int(row["branched_at_seq"] or 0)
            sid = parent_id
        chain.reverse()
        return chain

    def _materialize_events_sync(self, session_id: str) -> list[EventRecord]:
        chain = self._walk_chain_sync(session_id)
        conn = self._conn()
        out: list[EventRecord] = []
        for sid, upper in chain:
            if upper is None:
                rows = conn.execute(
                    "SELECT session_id, seq, kind, payload, timestamp "
                    "FROM events WHERE session_id = ? ORDER BY seq",
                    (sid,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT session_id, seq, kind, payload, timestamp "
                    "FROM events WHERE session_id = ? AND seq <= ? ORDER BY seq",
                    (sid, upper),
                ).fetchall()
            for row in rows:
                out.append(
                    EventRecord(
                        session_id=row["session_id"],
                        seq=row["seq"],
                        kind=AgentEventKind(row["kind"]),
                        payload=_deserialize_payload(row["payload"]),
                        timestamp=datetime.fromisoformat(row["timestamp"]),
                    ),
                )
        return out

    def _write_snapshot_sync(
        self,
        session_id: str,
        up_to_seq: int,
        context_blob: str,
        format_version: int,
    ) -> SnapshotRecord:
        now = datetime.now(UTC).isoformat()
        conn = self._conn()
        conn.execute(
            "INSERT OR REPLACE INTO session_snapshots "
            "(session_id, up_to_seq, context_blob, format_version, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (session_id, up_to_seq, context_blob, format_version, now),
        )
        conn.commit()
        return SnapshotRecord(
            session_id=session_id,
            up_to_seq=up_to_seq,
            context_blob=context_blob,
            format_version=format_version,
            created_at=datetime.fromisoformat(now),
        )

    def _get_nearest_snapshot_sync(
        self,
        session_id: str,
        max_seq: int | None,
    ) -> SnapshotRecord | None:
        chain = self._walk_chain_sync(session_id)
        conn = self._conn()
        # Walk leaf -> root: prefer the deepest hit.
        for sid, upper in reversed(chain):
            if sid == session_id:
                bound = max_seq
            else:
                bound = upper
            if bound is None:
                row = conn.execute(
                    "SELECT session_id, up_to_seq, context_blob, format_version, created_at "
                    "FROM session_snapshots WHERE session_id = ? "
                    "ORDER BY up_to_seq DESC LIMIT 1",
                    (sid,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT session_id, up_to_seq, context_blob, format_version, created_at "
                    "FROM session_snapshots WHERE session_id = ? AND up_to_seq <= ? "
                    "ORDER BY up_to_seq DESC LIMIT 1",
                    (sid, bound),
                ).fetchone()
            if row is not None:
                return SnapshotRecord(
                    session_id=row["session_id"],
                    up_to_seq=int(row["up_to_seq"]),
                    context_blob=row["context_blob"],
                    format_version=int(row["format_version"]),
                    created_at=datetime.fromisoformat(row["created_at"]),
                )
        return None

    def _list_snapshots_sync(self, session_id: str) -> list[SnapshotRecord]:
        rows = (
            self._conn()
            .execute(
                "SELECT session_id, up_to_seq, context_blob, format_version, created_at "
                "FROM session_snapshots WHERE session_id = ? ORDER BY up_to_seq",
                (session_id,),
            )
            .fetchall()
        )
        return [
            SnapshotRecord(
                session_id=row["session_id"],
                up_to_seq=int(row["up_to_seq"]),
                context_blob=row["context_blob"],
                format_version=int(row["format_version"]),
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            for row in rows
        ]

    def _materialize_prefix_into_child_sync(self, child_session_id: str) -> int:
        conn = self._conn()
        child_row = conn.execute(
            f"SELECT {_SESSION_COLS} FROM sessions WHERE id = ?",
            (child_session_id,),
        ).fetchone()
        if child_row is None:
            msg = f"Session not found: {child_session_id}"
            raise ValueError(msg)
        child = _row_to_session(child_row)
        if not child.parent_session_id:
            return 0

        parent_row = conn.execute(
            "SELECT parent_session_id FROM sessions WHERE id = ?",
            (child.parent_session_id,),
        ).fetchone()
        branched_at = child.branched_at_seq
        if parent_row is not None and (parent_row["parent_session_id"] or ""):
            inlined = self._materialize_prefix_into_child_sync(child.parent_session_id)
            # Parent's events were shifted up by `inlined`; our cut point shifts too.
            branched_at += inlined

        conn.execute("BEGIN IMMEDIATE")
        try:
            parent_events = conn.execute(
                "SELECT seq, kind, payload, timestamp FROM events "
                "WHERE session_id = ? AND seq <= ? ORDER BY seq",
                (child.parent_session_id, branched_at),
            ).fetchall()
            n = len(parent_events)

            if n > 0:
                child_events = conn.execute(
                    "SELECT seq, kind, payload, timestamp FROM events "
                    "WHERE session_id = ? ORDER BY seq DESC",
                    (child_session_id,),
                ).fetchall()
                # Shift child events up by n, descending order so we don't
                # collide with rows we haven't moved yet.
                for row in child_events:
                    conn.execute(
                        "UPDATE events SET seq = ? WHERE session_id = ? AND seq = ?",
                        (int(row["seq"]) + n, child_session_id, int(row["seq"])),
                    )

                for row in parent_events:
                    conn.execute(
                        "INSERT INTO events (session_id, seq, kind, payload, timestamp) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (
                            child_session_id,
                            int(row["seq"]),
                            row["kind"],
                            row["payload"],
                            row["timestamp"],
                        ),
                    )

            now = datetime.now(UTC).isoformat()
            conn.execute(
                "UPDATE sessions SET parent_session_id = '', branched_at_seq = 0, "
                "updated_at = ? WHERE id = ?",
                (now, child_session_id),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return n

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
        metadata: dict[str, Any] | None = None,
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

    # ------------------------------------------------------------------
    # Session reaper — clean up orphaned sessions from crashed processes
    # ------------------------------------------------------------------

    def _reap_orphaned_sessions_sync(self) -> int:
        from obscura.core.session_utils import list_active_sessions

        conn = self._conn()
        rows = conn.execute(
            "SELECT id FROM sessions WHERE status IN ('running', 'waiting_for_tool', 'waiting_for_user')"
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
                    "UPDATE sessions SET status = ?, updated_at = ? WHERE id = ?",
                    (SessionStatus.FAILED.value, now, sid),
                )
                reaped += 1

        if reaped:
            conn.commit()
        return reaped

    async def reap_orphaned_sessions(self) -> int:
        return await asyncio.to_thread(self._reap_orphaned_sessions_sync)

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
        parent_session_id: str | None = None,
    ) -> list[SessionRecord]:
        return await asyncio.to_thread(
            self._list_sessions_sync,
            status,
            backend,
            source,
            parent_session_id,
        )

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
            new_session_id=new_session_id,
            agent=agent,
            backend=backend,
            model=model,
            summary=summary,
            metadata=metadata,
        )

    async def freeze_session(self, session_id: str) -> None:
        await asyncio.to_thread(self._freeze_session_sync, session_id)

    async def materialize_events(self, session_id: str) -> list[EventRecord]:
        return await asyncio.to_thread(self._materialize_events_sync, session_id)

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

    async def list_snapshots(self, session_id: str) -> list[SnapshotRecord]:
        return await asyncio.to_thread(self._list_snapshots_sync, session_id)

    async def materialize_prefix_into_child(self, child_session_id: str) -> int:
        return await asyncio.to_thread(
            self._materialize_prefix_into_child_sync,
            child_session_id,
        )

    def close(self) -> None:
        """Close the thread-local connection."""
        if hasattr(self._local, "conn") and self._local.conn is not None:
            self._local.conn.close()
            self._local.conn = None
