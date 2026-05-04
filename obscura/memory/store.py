"""obscura.memory.store — SQLite-backed MemoryStore + GlobalMemoryStore.

Holds the SQLite implementation that used to live inside
``obscura/memory/__init__.py``. Sits above ``obscura.memory.types`` and
``obscura.memory.events`` (both leaf modules), so its imports of those
modules can stay top-level — no lazy escape hatch needed.

The factory ``create_memory_store`` switches to
``obscura.memory.postgres_memory.PostgreSQLMemoryStore`` when
``OBSCURA_DB_TYPE=postgresql``; that import is also top-level now that
``postgres_memory`` depends only on ``obscura.memory.types``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, override

from obscura.core.pg_config import is_pg_configured
from obscura.memory.events import (
    EventKind,
    EventSink,
    EventSource,
    get_default_sink,
    make_event,
)
from obscura.memory.postgres_memory import PostgreSQLMemoryStore
from obscura.memory.types import MemoryEntry, MemoryKey

if TYPE_CHECKING:
    from obscura.auth.models import AuthenticatedUser

logger = logging.getLogger(__name__)

__all__ = [
    "GlobalMemoryStore",
    "MemoryEntry",
    "MemoryKey",
    "MemoryStore",
    "create_memory_store",
]


class MemoryStore:
    """Per-user memory store scoped by auth token.

    Each user gets an isolated SQLite database identified by their user_id.
    Supports namespaces for organizing memory (session, project, user, global).
    """

    _instances: dict[str, MemoryStore] = {}
    _lock = threading.Lock()

    def __init__(
        self,
        user: AuthenticatedUser,
        db_path: Path | None = None,
        event_sink: EventSink | None = None,
    ) -> None:
        self.user = user
        self.user_id = user.user_id

        # Hash the user_id for filesystem safety
        self._db_id = hashlib.sha256(self.user_id.encode()).hexdigest()[:16]

        if db_path is None:
            # Default: ~/.obscura/memory/<user_hash>.db, overrideable for tests
            base_dir = Path(
                os.environ.get(
                    "OBSCURA_MEMORY_DIR",
                    Path.home() / ".obscura" / "memory",
                ),
            )
            db_path = base_dir / f"{self._db_id}.db"

        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self._local = threading.local()
        self._event_sink = event_sink
        self._init_db()

    def _emit(
        self,
        kind: str,
        key: MemoryKey,
        value: Any | None,
        ttl_seconds: float | None,
    ) -> None:
        """Emit a memory event."""
        sink = self._event_sink if self._event_sink is not None else get_default_sink()
        sink.emit(
            make_event(
                kind=EventKind(kind),
                key=key,
                value=value,
                ttl_seconds=ttl_seconds,
                source=EventSource.KV,
                user_id=self.user_id,
            ),
        )

    @classmethod
    def for_user(cls, user: AuthenticatedUser) -> MemoryStore:
        """Get or create a memory store for the given user."""
        with cls._lock:
            if user.user_id not in cls._instances:
                cls._instances[user.user_id] = cls(user)
            return cls._instances[user.user_id]

    @classmethod
    def reset_instances(cls) -> None:
        """Clear singleton cache. For testing only."""
        with cls._lock:
            cls._instances.clear()

    def _get_conn(self) -> sqlite3.Connection:
        """Get thread-local database connection."""
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = sqlite3.connect(self.db_path)
            self._local.conn.row_factory = sqlite3.Row
        return self._local.conn

    def _init_db(self) -> None:
        """Initialize the database schema."""
        conn = self._get_conn()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS memory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                namespace TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP,
                UNIQUE(namespace, key)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_memory_ns_key ON memory(namespace, key)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_memory_expires ON memory(expires_at)
        """)
        conn.commit()

    def set(
        self,
        key: str | MemoryKey,
        value: Any,
        namespace: str = "default",
        ttl: timedelta | None = None,
    ) -> None:
        """Store a value in memory.

        Args:
            key: The memory key (or MemoryKey)
            value: Any JSON-serializable value
            namespace: Logical grouping (session, project, user, etc.)
            ttl: Optional time-to-live

        """
        if isinstance(key, str):
            key = MemoryKey(namespace=namespace, key=key)

        expires_at = None
        if ttl:
            expires_at = datetime.now(UTC) + ttl

        conn = self._get_conn()
        conn.execute(
            """
            INSERT INTO memory (namespace, key, value, expires_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(namespace, key) DO UPDATE SET
                value = excluded.value,
                updated_at = CURRENT_TIMESTAMP,
                expires_at = excluded.expires_at
            """,
            (key.namespace, key.key, json.dumps(value), expires_at),
        )
        conn.commit()
        self._emit(
            "set",
            key,
            value,
            ttl.total_seconds() if ttl else None,
        )

    def get(
        self,
        key: str | MemoryKey,
        namespace: str = "default",
        default: Any = None,
    ) -> Any:
        """Retrieve a value from memory.

        Returns default if key not found or expired.
        """
        if isinstance(key, str):
            key = MemoryKey(namespace=namespace, key=key)

        conn = self._get_conn()
        row = conn.execute(
            "SELECT value, expires_at FROM memory WHERE namespace = ? AND key = ?",
            (key.namespace, key.key),
        ).fetchone()

        if row is None:
            return default

        # Check expiration
        if row["expires_at"]:
            expires = datetime.fromisoformat(row["expires_at"])
            if datetime.now(UTC) > expires:
                # Rowcount guard: only the thread that actually removes the
                # row emits the event. Prevents double-emit when lazy-expire
                # races with the reaper or a concurrent get().
                if self._delete_row(key):
                    self._emit("expire", key, None, None)
                return default

        return json.loads(row["value"])

    def delete(self, key: str | MemoryKey, namespace: str = "default") -> bool:
        """Delete a key from memory. Returns True if key existed."""
        if isinstance(key, str):
            key = MemoryKey(namespace=namespace, key=key)

        existed = self._delete_row(key)
        if existed:
            self._emit("delete", key, None, None)
        return existed

    def _delete_row(self, key: MemoryKey) -> bool:
        """Remove the row without emitting an event. Used by delete() and expire."""
        conn = self._get_conn()
        cursor = conn.execute(
            "DELETE FROM memory WHERE namespace = ? AND key = ?",
            (key.namespace, key.key),
        )
        conn.commit()
        return cursor.rowcount > 0

    def list_keys(self, namespace: str | None = None) -> list[MemoryKey]:
        """List all keys, optionally filtered by namespace."""
        conn = self._get_conn()

        if namespace:
            rows = conn.execute(
                "SELECT namespace, key FROM memory WHERE namespace = ?",
                (namespace,),
            ).fetchall()
        else:
            rows = conn.execute("SELECT namespace, key FROM memory").fetchall()

        return [MemoryKey(namespace=r["namespace"], key=r["key"]) for r in rows]

    def search(self, query: str) -> list[tuple[MemoryKey, Any]]:
        """Simple text search over keys and string values.

        For semantic search, use the vector memory extension.
        """
        conn = self._get_conn()
        pattern = f"%{query}%"
        rows = conn.execute(
            """
            SELECT namespace, key, value FROM memory
            WHERE key LIKE ? OR value LIKE ?
            """,
            (pattern, pattern),
        ).fetchall()

        results: list[tuple[MemoryKey, Any]] = []
        for row in rows:
            key = MemoryKey(namespace=row["namespace"], key=row["key"])
            try:
                value: Any = json.loads(row["value"])
            except json.JSONDecodeError:
                logger.debug("suppressed exception in search", exc_info=True)
                value = row["value"]
            results.append((key, value))

        return results

    def clear_namespace(self, namespace: str) -> int:
        """Clear all keys in a namespace. Returns count deleted."""
        conn = self._get_conn()
        cursor = conn.execute("DELETE FROM memory WHERE namespace = ?", (namespace,))
        conn.commit()
        return cursor.rowcount

    def clear_expired(self) -> int:
        """Clear all expired entries. Returns count deleted.

        Does NOT emit events. Use :meth:`reap_expired` if you want the
        reaper-style per-row event emission.
        """
        conn = self._get_conn()
        cursor = conn.execute(
            "DELETE FROM memory WHERE expires_at IS NOT NULL AND expires_at < ?",
            (datetime.now(UTC),),
        )
        conn.commit()
        return cursor.rowcount

    def _expired_keys(self) -> list[MemoryKey]:
        """Return keys that have passed their ``expires_at``.

        Used by the reaper to select candidates before the delete+emit pair.
        The actual delete races with other threads; emission is gated on
        whether *this* caller's delete actually removed the row.
        """
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT namespace, key FROM memory "
            "WHERE expires_at IS NOT NULL AND expires_at < ?",
            (datetime.now(UTC),),
        ).fetchall()
        return [MemoryKey(namespace=r["namespace"], key=r["key"]) for r in rows]

    def reap_expired(self) -> int:
        """Delete expired rows and emit an ``expire`` event for each one.

        Returns the number of rows *this* call reaped (races with lazy-expire
        in :meth:`get` are resolved via rowcount: whichever deleter wins emits).
        """
        reaped = 0
        for key in self._expired_keys():
            if self._delete_row(key):
                self._emit("expire", key, None, None)
                reaped += 1
        return reaped

    def get_stats(self) -> dict[str, Any]:
        """Get memory usage statistics."""
        conn = self._get_conn()

        total = conn.execute("SELECT COUNT(*) as count FROM memory").fetchone()["count"]
        expired = conn.execute(
            "SELECT COUNT(*) as count FROM memory WHERE expires_at IS NOT NULL AND expires_at < ?",
            (datetime.now(UTC),),
        ).fetchone()["count"]

        namespaces = conn.execute(
            "SELECT namespace, COUNT(*) as count FROM memory GROUP BY namespace",
        ).fetchall()

        return {
            "total_keys": total,
            "expired_keys": expired,
            "namespaces": {r["namespace"]: r["count"] for r in namespaces},
            "db_path": str(self.db_path),
        }

    def close(self) -> None:
        """Close the database connection."""
        if hasattr(self._local, "conn") and self._local.conn:
            self._local.conn.close()
            self._local.conn = None


class GlobalMemoryStore(MemoryStore):
    """Shared global memory accessible to all users (read-only for most).

    Useful for storing organization-wide knowledge, shared skills, etc.
    """

    _instance: GlobalMemoryStore | None = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        # Don't call super().__init__ to avoid auth requirement
        self._db_id = "global"
        self.user_id = "__global__"
        self.db_path = Path.home() / ".obscura" / "memory" / "global.db"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self._local = threading.local()
        self._event_sink = None
        self._init_db()

    @classmethod
    def get_instance(cls) -> GlobalMemoryStore:
        """Get the singleton global memory store."""
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    @override
    def set(self, *args: Any, **kwargs: Any) -> None:
        """Override to add audit logging for global writes."""
        # TODO: Add audit logging
        super().set(*args, **kwargs)


def create_memory_store(user: AuthenticatedUser) -> MemoryStore:
    """Factory: return a PostgreSQL or SQLite memory store based on config.

    When ``OBSCURA_DB_TYPE=postgresql``, returns a
    :class:`~obscura.memory.postgres_memory.PostgreSQLMemoryStore`.
    Otherwise returns the default SQLite-backed :class:`MemoryStore`.
    """
    if is_pg_configured():
        return PostgreSQLMemoryStore.for_user(user)  # type: ignore[return-value]
    return MemoryStore.for_user(user)
