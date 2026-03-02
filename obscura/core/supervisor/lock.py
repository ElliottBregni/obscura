"""
obscura.core.supervisor.lock — Cross-process session locking via SQLite.

Uses SQLite's own serialization (BEGIN IMMEDIATE) to implement advisory
locks. No file locks needed — works on any filesystem including NFS/PVC.

Lock lifecycle:
    acquire() → heartbeat() → ... → release()

On crash, locks expire after TTL and can be stolen by a new holder.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from obscura.core.supervisor.errors import LockAcquisitionError, LockExpiredError
from obscura.core.supervisor.schema import init_supervisor_schema
from obscura.core.supervisor.types import LockInfo, SupervisorEvent, SupervisorEventKind

logger = logging.getLogger(__name__)


class SessionLock:
    """SQLite-based advisory lock for single-writer session semantics.

    Each lock holder gets a unique ``holder_id`` (UUID). The lock is
    stored in the ``session_locks`` table with a TTL. If the holder
    crashes, the lock expires and can be stolen by another process.

    Thread-safe. All public methods are async (DB ops via ``asyncio.to_thread``).

    Usage::

        lock = SessionLock(db_path="/tmp/supervisor.db")
        holder_id = str(uuid.uuid4())

        info = await lock.acquire("sess-1", holder_id, timeout=30.0)
        try:
            # ... do work ...
            await lock.heartbeat("sess-1", holder_id)
        finally:
            await lock.release("sess-1", holder_id)
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        default_ttl: float = 60.0,
    ) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._default_ttl = default_ttl
        self._local = threading.local()
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
        conn = self._conn()
        init_supervisor_schema(conn)

    # -- sync helpers --------------------------------------------------------

    def _acquire_sync(
        self,
        session_id: str,
        holder_id: str,
        *,
        ttl: float | None = None,
    ) -> LockInfo | None:
        """Try to acquire the lock. Returns LockInfo on success, None if held."""
        ttl = ttl or self._default_ttl
        conn = self._conn()
        now = datetime.now(UTC)
        expires = now + timedelta(seconds=ttl)

        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError:
            # Another writer is active
            return None

        try:
            row = conn.execute(
                "SELECT holder_id, expires_at FROM session_locks WHERE session_id = ?",
                (session_id,),
            ).fetchone()

            if row is not None:
                existing_holder = row["holder_id"]
                existing_expires = datetime.fromisoformat(row["expires_at"])

                if existing_holder == holder_id:
                    # Re-entrant: we already hold it
                    conn.execute(
                        "UPDATE session_locks SET heartbeat_at = ?, expires_at = ? "
                        "WHERE session_id = ?",
                        (now.isoformat(), expires.isoformat(), session_id),
                    )
                    conn.commit()
                    return LockInfo(
                        session_id=session_id,
                        holder_id=holder_id,
                        acquired_at=now,
                        heartbeat_at=now,
                        expires_at=expires,
                    )

                if datetime.now(UTC) < existing_expires:
                    # Lock is held and not expired
                    conn.rollback()
                    return None

                # Lock expired — steal it
                logger.warning(
                    "Stealing expired lock for session %s from %s",
                    session_id,
                    existing_holder,
                )
                conn.execute(
                    "UPDATE session_locks SET holder_id = ?, acquired_at = ?, "
                    "heartbeat_at = ?, expires_at = ? WHERE session_id = ?",
                    (
                        holder_id,
                        now.isoformat(),
                        now.isoformat(),
                        expires.isoformat(),
                        session_id,
                    ),
                )
                conn.commit()
                return LockInfo(
                    session_id=session_id,
                    holder_id=holder_id,
                    acquired_at=now,
                    heartbeat_at=now,
                    expires_at=expires,
                )

            # No lock exists — create one
            conn.execute(
                "INSERT INTO session_locks (session_id, holder_id, acquired_at, "
                "heartbeat_at, expires_at) VALUES (?, ?, ?, ?, ?)",
                (
                    session_id,
                    holder_id,
                    now.isoformat(),
                    now.isoformat(),
                    expires.isoformat(),
                ),
            )
            conn.commit()
            return LockInfo(
                session_id=session_id,
                holder_id=holder_id,
                acquired_at=now,
                heartbeat_at=now,
                expires_at=expires,
            )

        except Exception:
            conn.rollback()
            raise

    def _release_sync(self, session_id: str, holder_id: str) -> bool:
        """Release a lock. Returns True if released, False if not held."""
        conn = self._conn()
        cursor = conn.execute(
            "DELETE FROM session_locks WHERE session_id = ? AND holder_id = ?",
            (session_id, holder_id),
        )
        conn.commit()
        released = cursor.rowcount > 0
        if released:
            logger.debug("Released lock for session %s", session_id)
        return released

    def _heartbeat_sync(
        self,
        session_id: str,
        holder_id: str,
        *,
        ttl: float | None = None,
    ) -> bool:
        """Refresh the lock TTL. Returns True if refreshed, False if not held."""
        ttl = ttl or self._default_ttl
        conn = self._conn()
        now = datetime.now(UTC)
        expires = now + timedelta(seconds=ttl)

        cursor = conn.execute(
            "UPDATE session_locks SET heartbeat_at = ?, expires_at = ? "
            "WHERE session_id = ? AND holder_id = ?",
            (now.isoformat(), expires.isoformat(), session_id, holder_id),
        )
        conn.commit()
        return cursor.rowcount > 0

    def _get_lock_sync(self, session_id: str) -> LockInfo | None:
        """Get current lock info for a session."""
        row = self._conn().execute(
            "SELECT session_id, holder_id, acquired_at, heartbeat_at, expires_at "
            "FROM session_locks WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        return LockInfo(
            session_id=row["session_id"],
            holder_id=row["holder_id"],
            acquired_at=datetime.fromisoformat(row["acquired_at"]),
            heartbeat_at=datetime.fromisoformat(row["heartbeat_at"]),
            expires_at=datetime.fromisoformat(row["expires_at"]),
        )

    def _cleanup_expired_sync(self) -> int:
        """Remove all expired locks. Returns count of cleaned locks."""
        conn = self._conn()
        now = datetime.now(UTC).isoformat()
        cursor = conn.execute(
            "DELETE FROM session_locks WHERE expires_at < ?", (now,)
        )
        conn.commit()
        count = cursor.rowcount
        if count > 0:
            logger.info("Cleaned up %d expired locks", count)
        return count

    # -- async public API ----------------------------------------------------

    async def acquire(
        self,
        session_id: str,
        holder_id: str,
        *,
        timeout: float = 30.0,
        ttl: float | None = None,
        poll_interval: float = 0.5,
    ) -> LockInfo:
        """Acquire the session lock, waiting up to ``timeout`` seconds.

        Raises:
            LockAcquisitionError: If lock cannot be acquired within timeout.
        """
        deadline = time.monotonic() + timeout
        wait_start = time.monotonic()

        while True:
            info = await asyncio.to_thread(
                self._acquire_sync, session_id, holder_id, ttl=ttl
            )
            if info is not None:
                wait_ms = (time.monotonic() - wait_start) * 1000
                logger.debug(
                    "Acquired lock for session %s (wait: %.0fms)",
                    session_id,
                    wait_ms,
                )
                return info

            if time.monotonic() >= deadline:
                # Check who holds the lock for error reporting
                current = await asyncio.to_thread(self._get_lock_sync, session_id)
                raise LockAcquisitionError(
                    session_id,
                    holder_id=current.holder_id if current else "",
                    timeout=timeout,
                )

            await asyncio.sleep(poll_interval)

    async def release(self, session_id: str, holder_id: str) -> bool:
        """Release the session lock."""
        return await asyncio.to_thread(self._release_sync, session_id, holder_id)

    async def heartbeat(
        self,
        session_id: str,
        holder_id: str,
        *,
        ttl: float | None = None,
    ) -> bool:
        """Refresh the lock TTL (heartbeat)."""
        return await asyncio.to_thread(
            self._heartbeat_sync, session_id, holder_id, ttl=ttl
        )

    async def get_lock(self, session_id: str) -> LockInfo | None:
        """Get current lock info."""
        return await asyncio.to_thread(self._get_lock_sync, session_id)

    async def cleanup_expired(self) -> int:
        """Remove all expired locks."""
        return await asyncio.to_thread(self._cleanup_expired_sync)

    async def is_locked(self, session_id: str) -> bool:
        """Check if a session is currently locked (non-expired)."""
        info = await self.get_lock(session_id)
        if info is None:
            return False
        return not info.is_expired

    def close(self) -> None:
        """Close the thread-local connection."""
        if hasattr(self._local, "conn") and self._local.conn is not None:
            self._local.conn.close()
            self._local.conn = None
