"""PostgreSQL-backed memory store for Obscura agents.

Drop-in replacement for the SQLite-based :class:`MemoryStore`.
All users share one database with ``user_id`` as a discriminator
(SQLite uses per-user files).

Usage::

    from obscura.memory.postgres_memory import PostgreSQLMemoryStore

    store = PostgreSQLMemoryStore.for_user(user)
    store.set("project_context", {"repo": "obscura"})
    ctx = store.get("project_context")
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

from obscura.core.pg_config import PGPoolManager
from obscura.memory import MemoryKey

if TYPE_CHECKING:
    from obscura.auth.models import AuthenticatedUser

logger = logging.getLogger(__name__)


class PostgreSQLMemoryStore:
    """PostgreSQL-backed key-value memory store.

    Mirrors the public API of :class:`obscura.memory.MemoryStore` but
    stores everything in a shared PostgreSQL database.
    """

    _instances: dict[str, PostgreSQLMemoryStore] = {}
    _lock = threading.Lock()
    _schema_initialized = False

    def __init__(self, user: AuthenticatedUser) -> None:
        self.user = user
        self.user_id = user.user_id
        self._pool = PGPoolManager.get_pool()
        self._ensure_schema()

    @classmethod
    def for_user(cls, user: AuthenticatedUser) -> PostgreSQLMemoryStore:
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
            cls._schema_initialized = False

    def _get_conn(self) -> Any:
        return self._pool.getconn()

    def _put_conn(self, conn: Any) -> None:
        self._pool.putconn(conn)

    def _ensure_schema(self) -> None:
        if PostgreSQLMemoryStore._schema_initialized:
            return
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE SCHEMA IF NOT EXISTS memory;
                    CREATE TABLE IF NOT EXISTS memory.entries (
                        id SERIAL PRIMARY KEY,
                        user_id TEXT NOT NULL,
                        namespace TEXT NOT NULL,
                        key TEXT NOT NULL,
                        value JSONB NOT NULL,
                        created_at TIMESTAMPTZ DEFAULT NOW(),
                        updated_at TIMESTAMPTZ DEFAULT NOW(),
                        expires_at TIMESTAMPTZ,
                        UNIQUE(user_id, namespace, key)
                    );
                    CREATE INDEX IF NOT EXISTS idx_memory_user_ns_key
                        ON memory.entries(user_id, namespace, key);
                    CREATE INDEX IF NOT EXISTS idx_memory_expires
                        ON memory.entries(expires_at);
                """)
            conn.commit()
            PostgreSQLMemoryStore._schema_initialized = True
        finally:
            self._put_conn(conn)

    # -- public API (mirrors MemoryStore) ------------------------------------

    def set(
        self,
        key: str,
        value: Any,
        namespace: str = "default",
        ttl: timedelta | None = None,
    ) -> None:
        """Store a value in memory."""
        if isinstance(key, MemoryKey):
            namespace = key.namespace
            key = key.key

        expires_at = None
        if ttl:
            expires_at = datetime.now(UTC) + ttl

        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO memory.entries (user_id, namespace, key, value, expires_at)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (user_id, namespace, key) DO UPDATE SET
                        value = EXCLUDED.value,
                        updated_at = NOW(),
                        expires_at = EXCLUDED.expires_at
                    """,
                    (self.user_id, namespace, key, json.dumps(value), expires_at),
                )
            conn.commit()
        finally:
            self._put_conn(conn)

    def get(
        self,
        key: str,
        namespace: str = "default",
        default: Any = None,
    ) -> Any:
        """Retrieve a value from memory. Returns default if not found or expired."""
        if isinstance(key, MemoryKey):
            namespace = key.namespace
            key = key.key

        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT value, expires_at FROM memory.entries "
                    "WHERE user_id = %s AND namespace = %s AND key = %s",
                    (self.user_id, namespace, key),
                )
                row = cur.fetchone()
        finally:
            self._put_conn(conn)

        if row is None:
            return default

        value_raw, expires_at = row["value"], row["expires_at"]
        if expires_at and datetime.now(UTC) > expires_at:
            self.delete(key, namespace=namespace)
            return default

        if isinstance(value_raw, (dict, list)):
            return cast(Any, value_raw)
        return json.loads(value_raw)

    def delete(self, key: str, namespace: str = "default") -> bool:
        """Delete a key. Returns True if deleted."""
        if isinstance(key, MemoryKey):
            namespace = key.namespace
            key = key.key

        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM memory.entries "
                    "WHERE user_id = %s AND namespace = %s AND key = %s",
                    (self.user_id, namespace, key),
                )
            conn.commit()
            return cur.rowcount > 0
        finally:
            self._put_conn(conn)

    def list_keys(self, namespace: str | None = None) -> list[str]:
        """List all keys, optionally filtered by namespace."""
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                if namespace:
                    cur.execute(
                        "SELECT key FROM memory.entries "
                        "WHERE user_id = %s AND namespace = %s "
                        "ORDER BY key",
                        (self.user_id, namespace),
                    )
                else:
                    cur.execute(
                        "SELECT key FROM memory.entries "
                        "WHERE user_id = %s ORDER BY key",
                        (self.user_id,),
                    )
                return [row["key"] for row in cur.fetchall()]
        finally:
            self._put_conn(conn)

    def search(self, query: str, namespace: str | None = None) -> list[dict[str, Any]]:
        """Search memory by key or value content (simple ILIKE)."""
        pattern = f"%{query}%"
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                if namespace:
                    cur.execute(
                        "SELECT namespace, key, value FROM memory.entries "
                        "WHERE user_id = %s AND namespace = %s "
                        "AND (key ILIKE %s OR value::text ILIKE %s) "
                        "ORDER BY updated_at DESC",
                        (self.user_id, namespace, pattern, pattern),
                    )
                else:
                    cur.execute(
                        "SELECT namespace, key, value FROM memory.entries "
                        "WHERE user_id = %s "
                        "AND (key ILIKE %s OR value::text ILIKE %s) "
                        "ORDER BY updated_at DESC",
                        (self.user_id, pattern, pattern),
                    )
                results: list[dict[str, Any]] = []
                for row in cur.fetchall():
                    val: Any = row["value"]
                    if isinstance(val, str):
                        val = json.loads(val)
                    results.append({
                        "namespace": row["namespace"],
                        "key": row["key"],
                        "value": val,
                    })
                return results
        finally:
            self._put_conn(conn)

    def clear_namespace(self, namespace: str) -> int:
        """Delete all keys in a namespace. Returns count deleted."""
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM memory.entries "
                    "WHERE user_id = %s AND namespace = %s",
                    (self.user_id, namespace),
                )
            conn.commit()
            return cur.rowcount
        finally:
            self._put_conn(conn)

    def clear_expired(self) -> int:
        """Remove all expired entries. Returns count deleted."""
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM memory.entries "
                    "WHERE user_id = %s AND expires_at IS NOT NULL AND expires_at < NOW()",
                    (self.user_id,),
                )
            conn.commit()
            return cur.rowcount
        finally:
            self._put_conn(conn)

    def get_stats(self) -> dict[str, Any]:
        """Get memory stats for this user."""
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) as total, "
                    "COUNT(DISTINCT namespace) as namespaces "
                    "FROM memory.entries WHERE user_id = %s",
                    (self.user_id,),
                )
                row = cur.fetchone()
                return {
                    "total_keys": row["total"],
                    "namespaces": row["namespaces"],
                    "backend": "postgresql",
                }
        finally:
            self._put_conn(conn)

    def close(self) -> None:
        """No-op — pool lifecycle managed by PGPoolManager."""
