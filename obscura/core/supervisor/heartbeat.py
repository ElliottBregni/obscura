"""
obscura.core.supervisor.heartbeat — Session-scoped heartbeat (first-class citizen).

Heartbeats are:
1. Persisted to ``session_heartbeats`` table (durable history)
2. Emitted as ``SupervisorEvent(HEARTBEAT)`` (event log)
3. Used to refresh the session lock TTL (liveness proof)

The heartbeat manager runs as an asyncio task during a supervised run,
ticking at a configurable interval.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import json
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Awaitable

from obscura.core.supervisor.schema import init_supervisor_schema
from obscura.core.supervisor.types import (
    SessionHeartbeat,
    SupervisorEvent,
    SupervisorEventKind,
    SupervisorState,
)

logger = logging.getLogger(__name__)

# Callback type: receives heartbeat, can be async or sync
HeartbeatCallback = Callable[[SessionHeartbeat], Awaitable[None] | None]


class SessionHeartbeatManager:
    """Manages heartbeat emission for a single supervised run.

    First-class session citizen: heartbeats are persisted events,
    not fire-and-forget pings.

    Usage::

        hb = SessionHeartbeatManager(
            db_path="/tmp/supervisor.db",
            session_id="sess-1",
            run_id="run-abc",
            interval=5.0,
        )
        hb.on_tick(my_lock_refresh_callback)

        await hb.start()
        # ... run executes ...
        hb.update_state(SupervisorState.RUNNING_TOOLS, turn=3)
        # ... later ...
        await hb.stop()
    """

    def __init__(
        self,
        db_path: str | Path,
        session_id: str,
        run_id: str,
        *,
        interval: float = 5.0,
    ) -> None:
        self._db_path = Path(db_path)
        self._session_id = session_id
        self._run_id = run_id
        self._interval = interval

        self._local = threading.local()
        self._seq = 0
        self._state = SupervisorState.IDLE
        self._turn_number = 0
        self._started_at: float | None = None
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self._callbacks: list[HeartbeatCallback] = []
        self._events: list[SupervisorEvent] = []

        self._init_schema()

    def _conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            conn = sqlite3.connect(str(self._db_path))
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
        return self._local.conn

    def _init_schema(self) -> None:
        init_supervisor_schema(self._conn())

    # -- state updates (called by supervisor) --------------------------------

    def update_state(self, state: SupervisorState, turn: int = 0) -> None:
        """Update the current state reported in heartbeats."""
        self._state = state
        self._turn_number = turn

    def on_tick(self, callback: HeartbeatCallback) -> None:
        """Register a callback invoked on each heartbeat tick.

        Typically used to refresh the session lock TTL.
        """
        self._callbacks.append(callback)

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        """Start the heartbeat loop."""
        if self._running:
            return
        self._running = True
        self._started_at = time.monotonic()
        self._task = asyncio.create_task(self._loop())
        logger.debug(
            "Heartbeat started for session %s, run %s (interval=%.1fs)",
            self._session_id,
            self._run_id,
            self._interval,
        )

    async def stop(self) -> None:
        """Stop the heartbeat loop and emit a final heartbeat."""
        if not self._running:
            return
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

        # Final heartbeat
        await self._tick()
        logger.debug(
            "Heartbeat stopped for session %s, run %s (total beats: %d)",
            self._session_id,
            self._run_id,
            self._seq,
        )

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def beat_count(self) -> int:
        return self._seq

    @property
    def events(self) -> list[SupervisorEvent]:
        """All heartbeat events emitted during this run."""
        return list(self._events)

    # -- internal ------------------------------------------------------------

    async def _loop(self) -> None:
        """Heartbeat loop. Ticks at interval until stopped."""
        while self._running:
            try:
                await self._tick()
            except Exception:
                logger.exception("Heartbeat tick failed")
            try:
                await asyncio.sleep(self._interval)
            except asyncio.CancelledError:
                break

    async def _tick(self) -> None:
        """Emit a single heartbeat."""
        self._seq += 1
        elapsed_ms = 0
        if self._started_at is not None:
            elapsed_ms = int((time.monotonic() - self._started_at) * 1000)

        heartbeat = SessionHeartbeat(
            session_id=self._session_id,
            run_id=self._run_id,
            seq=self._seq,
            state=self._state,
            turn_number=self._turn_number,
            elapsed_ms=elapsed_ms,
        )

        # Persist to DB
        await asyncio.to_thread(self._persist_heartbeat, heartbeat)

        # Create event
        event = SupervisorEvent(
            kind=SupervisorEventKind.HEARTBEAT,
            run_id=self._run_id,
            session_id=self._session_id,
            payload={
                "seq": heartbeat.seq,
                "state": heartbeat.state.value,
                "turn_number": heartbeat.turn_number,
                "elapsed_ms": heartbeat.elapsed_ms,
            },
        )
        self._events.append(event)

        # Notify callbacks (e.g., lock refresh)
        for callback in self._callbacks:
            try:
                result = callback(heartbeat)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                logger.exception("Heartbeat callback failed")

    def _persist_heartbeat(self, hb: SessionHeartbeat) -> None:
        """Write heartbeat to SQLite (sync, runs in thread)."""
        conn = self._conn()
        conn.execute(
            "INSERT OR REPLACE INTO session_heartbeats "
            "(session_id, run_id, seq, state, turn_number, elapsed_ms, "
            " timestamp, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                hb.session_id,
                hb.run_id,
                hb.seq,
                hb.state.value,
                hb.turn_number,
                hb.elapsed_ms,
                datetime.now(UTC).isoformat(),
                json.dumps(hb.metadata, default=str),
            ),
        )
        conn.commit()

    def close(self) -> None:
        """Close thread-local connection."""
        if hasattr(self._local, "conn") and self._local.conn is not None:
            self._local.conn.close()
            self._local.conn = None


# ---------------------------------------------------------------------------
# Query helpers (for observability / debugging)
# ---------------------------------------------------------------------------


def get_heartbeats_for_run(
    db_path: str | Path,
    run_id: str,
) -> list[SessionHeartbeat]:
    """Retrieve all heartbeats for a run (for debugging / tests)."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT session_id, run_id, seq, state, turn_number, elapsed_ms, "
        "timestamp, metadata FROM session_heartbeats "
        "WHERE run_id = ? ORDER BY seq",
        (run_id,),
    ).fetchall()
    conn.close()

    result: list[SessionHeartbeat] = []
    for row in rows:
        meta_raw = row["metadata"]
        meta: dict[str, Any] = {}
        if meta_raw:
            try:
                parsed = json.loads(meta_raw)
                if isinstance(parsed, dict):
                    meta = parsed
            except (json.JSONDecodeError, TypeError):
                pass
        result.append(
            SessionHeartbeat(
                session_id=row["session_id"],
                run_id=row["run_id"],
                seq=row["seq"],
                state=SupervisorState(row["state"]),
                turn_number=row["turn_number"],
                elapsed_ms=row["elapsed_ms"],
                timestamp=datetime.fromisoformat(row["timestamp"]),
                metadata=meta,
            )
        )
    return result
