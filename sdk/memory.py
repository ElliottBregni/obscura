"""
sdk/memory — Shared memory database for AI agents.

Multi-tenant memory storage scoped by auth token.
Agents can read/write key-value pairs, search semantically, and maintain
conversation history.

Usage::

    from sdk.memory import MemoryStore
    
    store = MemoryStore.for_user(user)
    store.set("project_context", {"repo": "obscura", "tech": "python"})
    context = store.get("project_context")
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sdk.auth.models import AuthenticatedUser


@dataclass(frozen=True)
class MemoryKey:
    """A namespaced memory key."""
    namespace: str  # e.g., "session", "project", "user", "global"
    key: str        # e.g., "context", "preferences", "history"
    
    def __str__(self) -> str:
        return f"{self.namespace}:{self.key}"


@dataclass
class MemoryEntry:
    """A single memory entry with metadata."""
    key: MemoryKey
    value: Any
    created_at: datetime
    updated_at: datetime
    ttl: timedelta | None = None  # Time-to-live for ephemeral memory
    
    @property
    def is_expired(self) -> bool:
        if self.ttl is None:
            return False
        return datetime.now(UTC) > self.updated_at + self.ttl


class MemoryStore:
    """
    Per-user memory store scoped by auth token.
    
    Each user gets an isolated SQLite database identified by their user_id.
    Supports namespaces for organizing memory (session, project, user, global).
    """
    
    _instances: dict[str, MemoryStore] = {}
    _lock = threading.Lock()
    
    def __init__(self, user: AuthenticatedUser, db_path: Path | None = None):
        self.user = user
        self.user_id = user.user_id
        
        # Hash the user_id for filesystem safety
        self._db_id = hashlib.sha256(self.user_id.encode()).hexdigest()[:16]
        
        if db_path is None:
            # Default: ~/.obscura/memory/<user_hash>.db
            db_path = Path.home() / ".obscura" / "memory" / f"{self._db_id}.db"
        
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        
        self._local = threading.local()
        self._init_db()
    
    @classmethod
    def for_user(cls, user: AuthenticatedUser) -> MemoryStore:
        """Get or create a memory store for the given user."""
        with cls._lock:
            if user.user_id not in cls._instances:
                cls._instances[user.user_id] = cls(user)
            return cls._instances[user.user_id]
    
    def _get_conn(self) -> sqlite3.Connection:
        """Get thread-local database connection."""
        if not hasattr(self._local, 'conn') or self._local.conn is None:
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
        ttl: timedelta | None = None
    ) -> None:
        """
        Store a value in memory.
        
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
            (key.namespace, key.key, json.dumps(value), expires_at)
        )
        conn.commit()
    
    def get(self, key: str | MemoryKey, namespace: str = "default", default: Any = None) -> Any:
        """
        Retrieve a value from memory.
        
        Returns default if key not found or expired.
        """
        if isinstance(key, str):
            key = MemoryKey(namespace=namespace, key=key)
        
        conn = self._get_conn()
        row = conn.execute(
            "SELECT value, expires_at FROM memory WHERE namespace = ? AND key = ?",
            (key.namespace, key.key)
        ).fetchone()
        
        if row is None:
            return default
        
        # Check expiration
        if row['expires_at']:
            expires = datetime.fromisoformat(row['expires_at'])
            if datetime.now(UTC) > expires:
                self.delete(key)
                return default
        
        return json.loads(row['value'])
    
    def delete(self, key: str | MemoryKey, namespace: str = "default") -> bool:
        """Delete a key from memory. Returns True if key existed."""
        if isinstance(key, str):
            key = MemoryKey(namespace=namespace, key=key)
        
        conn = self._get_conn()
        cursor = conn.execute(
            "DELETE FROM memory WHERE namespace = ? AND key = ?",
            (key.namespace, key.key)
        )
        conn.commit()
        return cursor.rowcount > 0
    
    def list_keys(self, namespace: str | None = None) -> list[MemoryKey]:
        """List all keys, optionally filtered by namespace."""
        conn = self._get_conn()
        
        if namespace:
            rows = conn.execute(
                "SELECT namespace, key FROM memory WHERE namespace = ?",
                (namespace,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT namespace, key FROM memory").fetchall()
        
        return [MemoryKey(namespace=r['namespace'], key=r['key']) for r in rows]
    
    def search(self, query: str) -> list[tuple[MemoryKey, Any]]:
        """
        Simple text search over keys and string values.
        
        For semantic search, use the vector memory extension.
        """
        conn = self._get_conn()
        pattern = f"%{query}%"
        rows = conn.execute(
            """
            SELECT namespace, key, value FROM memory
            WHERE key LIKE ? OR value LIKE ?
            """,
            (pattern, pattern)
        ).fetchall()
        
        results = []
        for row in rows:
            key = MemoryKey(namespace=row['namespace'], key=row['key'])
            try:
                value = json.loads(row['value'])
            except json.JSONDecodeError:
                value = row['value']
            results.append((key, value))
        
        return results
    
    def clear_namespace(self, namespace: str) -> int:
        """Clear all keys in a namespace. Returns count deleted."""
        conn = self._get_conn()
        cursor = conn.execute(
            "DELETE FROM memory WHERE namespace = ?",
            (namespace,)
        )
        conn.commit()
        return cursor.rowcount
    
    def clear_expired(self) -> int:
        """Clear all expired entries. Returns count deleted."""
        conn = self._get_conn()
        cursor = conn.execute(
            "DELETE FROM memory WHERE expires_at IS NOT NULL AND expires_at < ?",
            (datetime.now(UTC),)
        )
        conn.commit()
        return cursor.rowcount
    
    def get_stats(self) -> dict[str, Any]:
        """Get memory usage statistics."""
        conn = self._get_conn()
        
        total = conn.execute("SELECT COUNT(*) as count FROM memory").fetchone()['count']
        expired = conn.execute(
            "SELECT COUNT(*) as count FROM memory WHERE expires_at IS NOT NULL AND expires_at < ?",
            (datetime.now(UTC),)
        ).fetchone()['count']
        
        namespaces = conn.execute(
            "SELECT namespace, COUNT(*) as count FROM memory GROUP BY namespace"
        ).fetchall()
        
        return {
            "total_keys": total,
            "expired_keys": expired,
            "namespaces": {r['namespace']: r['count'] for r in namespaces},
            "db_path": str(self.db_path),
        }
    
    def close(self) -> None:
        """Close the database connection."""
        if hasattr(self._local, 'conn') and self._local.conn:
            self._local.conn.close()
            self._local.conn = None


class GlobalMemoryStore(MemoryStore):
    """
    Shared global memory accessible to all users (read-only for most).
    
    Useful for storing organization-wide knowledge, shared skills, etc.
    """
    
    _instance: GlobalMemoryStore | None = None
    _lock = threading.Lock()
    
    def __init__(self):
        # Don't call super().__init__ to avoid auth requirement
        self._db_id = "global"
        self.db_path = Path.home() / ".obscura" / "memory" / "global.db"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        
        self._local = threading.local()
        self._init_db()
    
    @classmethod
    def get(cls) -> GlobalMemoryStore:
        """Get the singleton global memory store."""
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance
    
    def set(self, *args, **kwargs) -> None:
        """Override to add audit logging for global writes."""
        # TODO: Add audit logging
        super().set(*args, **kwargs)
