"""SQLite-based vector memory backend (extracted from original implementation)."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from obscura.memory import MemoryKey
from obscura.vector_memory.backends.base import BackendConfig, VectorEntry
from obscura.vector_memory.vector_memory_filters import (
    MetadataFilter,
    match_metadata_filters,
)


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    a_arr = np.array(a)
    b_arr = np.array(b)

    norm_a = np.linalg.norm(a_arr)
    norm_b = np.linalg.norm(b_arr)

    if norm_a == 0 or norm_b == 0:
        return 0.0

    return float(np.dot(a_arr, b_arr) / (norm_a * norm_b))


class SQLiteBackend:
    """SQLite-based vector memory backend.

    Implements VectorBackend protocol using SQLite for storage.
    Thread-safe with per-thread connections.
    """

    def __init__(self, config: BackendConfig, db_path: Path | None = None):
        """Initialize SQLite backend."""
        self.config = config
        self._db_id = hashlib.sha256(config.user_id.encode()).hexdigest()[:16]

        if db_path is None:
            base_dir = Path(
                os.environ.get(
                    "OBSCURA_VECTOR_MEMORY_DIR",
                    Path.home() / ".obscura" / "vector_memory",
                ),
            )
            db_path = base_dir / f"{self._db_id}.db"

        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self._local = threading.local()
        self._init_db()

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
            CREATE TABLE IF NOT EXISTS vector_memory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                namespace TEXT NOT NULL,
                key TEXT NOT NULL,
                text TEXT NOT NULL,
                embedding BLOB NOT NULL,
                metadata TEXT,
                memory_type TEXT NOT NULL DEFAULT 'general',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP,
                UNIQUE(namespace, key)
            )
        """)

        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_vec_memory_ns_key ON vector_memory(namespace, key)",
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_vec_memory_expires ON vector_memory(expires_at)",
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_vec_memory_type ON vector_memory(memory_type)",
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_vec_memory_ns_created ON vector_memory(namespace, created_at DESC)",
        )

        conn.commit()

    def store_vector(
        self,
        key: MemoryKey,
        text: str,
        embedding: list[float],
        metadata: dict[str, Any],
        memory_type: str,
        expires_at: datetime | None,
    ) -> None:
        """Store a vector with metadata."""
        conn = self._get_conn()
        conn.execute(
            """
            INSERT INTO vector_memory (namespace, key, text, embedding, metadata, memory_type, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(namespace, key) DO UPDATE SET
                text = excluded.text,
                embedding = excluded.embedding,
                metadata = excluded.metadata,
                memory_type = excluded.memory_type,
                updated_at = CURRENT_TIMESTAMP,
                expires_at = excluded.expires_at
            """,
            (
                key.namespace,
                key.key,
                text,
                json.dumps(embedding),
                json.dumps(metadata) if metadata else None,
                memory_type,
                expires_at,
            ),
        )
        conn.commit()

    def get_vector(self, key: MemoryKey) -> VectorEntry | None:
        """Retrieve a vector by key."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM vector_memory WHERE namespace = ? AND key = ?",
            (key.namespace, key.key),
        ).fetchone()

        if row is None:
            return None

        if row["expires_at"]:
            expires = datetime.fromisoformat(row["expires_at"])
            if datetime.now(UTC) > expires:
                self.delete_vector(key)
                return None

        return VectorEntry(
            key=MemoryKey(namespace=row["namespace"], key=row["key"]),
            text=row["text"],
            embedding=json.loads(row["embedding"]),
            metadata=json.loads(row["metadata"]) if row["metadata"] else {},
            memory_type=row["memory_type"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"])
            if row["updated_at"]
            else None,
        )

    def search_vectors(
        self,
        query_embedding: list[float],
        namespace: str | None,
        top_k: int,
        threshold: float | None,
        filters: list[MetadataFilter] | None,
    ) -> list[VectorEntry]:
        """Search for similar vectors."""
        conn = self._get_conn()

        query = "SELECT * FROM vector_memory WHERE 1=1"
        params: list[Any] = []

        if namespace:
            query += " AND namespace = ?"
            params.append(namespace)

        query += " AND (expires_at IS NULL OR expires_at > ?)"
        params.append(datetime.now(UTC))

        rows = conn.execute(query, params).fetchall()

        results = []
        for row in rows:
            embedding = json.loads(row["embedding"])
            similarity = cosine_similarity(query_embedding, embedding)

            if threshold is not None and similarity < threshold:
                continue

            metadata = json.loads(row["metadata"]) if row["metadata"] else {}

            if filters and not match_metadata_filters(metadata, filters):
                continue

            entry = VectorEntry(
                key=MemoryKey(namespace=row["namespace"], key=row["key"]),
                text=row["text"],
                embedding=embedding,
                metadata=metadata,
                memory_type=row["memory_type"],
                created_at=datetime.fromisoformat(row["created_at"]),
                updated_at=datetime.fromisoformat(row["updated_at"])
                if row["updated_at"]
                else None,
                score=similarity,
            )
            results.append(entry)

        results.sort(key=lambda x: x.score, reverse=True)
        return results[:top_k]

    def delete_vector(self, key: MemoryKey) -> bool:
        """Delete a vector by key."""
        conn = self._get_conn()
        cursor = conn.execute(
            "DELETE FROM vector_memory WHERE namespace = ? AND key = ?",
            (key.namespace, key.key),
        )
        conn.commit()
        return cursor.rowcount > 0

    def list_keys(self, namespace: str | None = None) -> list[MemoryKey]:
        """List all keys."""
        conn = self._get_conn()

        if namespace:
            rows = conn.execute(
                "SELECT namespace, key FROM vector_memory WHERE namespace = ?",
                (namespace,),
            ).fetchall()
        else:
            rows = conn.execute("SELECT namespace, key FROM vector_memory").fetchall()

        return [MemoryKey(namespace=r["namespace"], key=r["key"]) for r in rows]

    def clear_namespace(self, namespace: str) -> int:
        """Clear all vectors in a namespace."""
        conn = self._get_conn()
        cursor = conn.execute(
            "DELETE FROM vector_memory WHERE namespace = ?",
            (namespace,),
        )
        conn.commit()
        return cursor.rowcount

    def get_stats(self) -> dict[str, Any]:
        """Get backend statistics."""
        conn = self._get_conn()

        total = conn.execute("SELECT COUNT(*) as count FROM vector_memory").fetchone()[
            "count"
        ]
        namespaces = conn.execute(
            "SELECT namespace, COUNT(*) as count FROM vector_memory GROUP BY namespace",
        ).fetchall()

        return {
            "backend": "sqlite",
            "total_vectors": total,
            "namespaces": {r["namespace"]: r["count"] for r in namespaces},
            "db_path": str(self.db_path),
            "embedding_dim": self.config.embedding_dim,
        }

    def close(self) -> None:
        """Close the database connection."""
        if hasattr(self._local, "conn") and self._local.conn:
            self._local.conn.close()
            self._local.conn = None
