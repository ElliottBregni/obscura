"""PostgreSQL-based vector memory backend.

Implements the :class:`VectorBackend` protocol using PostgreSQL.
Embeddings are stored as JSONB arrays; cosine similarity is computed
in Python (same approach as the SQLite backend).

For production deployments with large embedding collections, consider
using the ``pgvector`` extension with a dedicated GiST/IVFFlat index.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime
from typing import TYPE_CHECKING, Any

from obscura.core.pg_config import PGPoolManager
from obscura.memory import MemoryKey
from obscura.vector_memory.backends.base import BackendConfig, VectorEntry
from obscura.vector_memory.vector_memory_filters import match_metadata_filters

if TYPE_CHECKING:
    from obscura.vector_memory.vector_memory_filters import MetadataFilter

logger = logging.getLogger(__name__)


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


class PostgreSQLVectorBackend:
    """PostgreSQL-based vector memory backend.

    Implements the :class:`VectorBackend` protocol.
    """

    _schema_initialized = False
    _schema_lock = threading.Lock()

    def __init__(
        self,
        config: BackendConfig,
        **kwargs: Any,
    ) -> None:
        self.config = config
        self._user_id = config.user_id
        self._pool = PGPoolManager.get_pool()
        self._ensure_schema()

    def _get_conn(self) -> Any:
        return self._pool.getconn()

    def _put_conn(self, conn: Any) -> None:
        self._pool.putconn(conn)

    def _ensure_schema(self) -> None:
        if PostgreSQLVectorBackend._schema_initialized:
            return
        with PostgreSQLVectorBackend._schema_lock:
            if PostgreSQLVectorBackend._schema_initialized:
                return
            conn = self._get_conn()
            try:
                with conn.cursor() as cur:
                    cur.execute("""
                        CREATE SCHEMA IF NOT EXISTS vector_memory;
                        CREATE TABLE IF NOT EXISTS vector_memory.entries (
                            id SERIAL PRIMARY KEY,
                            user_id TEXT NOT NULL,
                            namespace TEXT NOT NULL,
                            key TEXT NOT NULL,
                            text TEXT NOT NULL,
                            embedding JSONB NOT NULL,
                            metadata JSONB,
                            memory_type TEXT NOT NULL DEFAULT 'general',
                            created_at TIMESTAMPTZ DEFAULT NOW(),
                            updated_at TIMESTAMPTZ DEFAULT NOW(),
                            accessed_at TIMESTAMPTZ,
                            expires_at TIMESTAMPTZ,
                            UNIQUE(user_id, namespace, key)
                        );
                        CREATE INDEX IF NOT EXISTS idx_vec_entries_user_ns_key
                            ON vector_memory.entries(user_id, namespace, key);
                        CREATE INDEX IF NOT EXISTS idx_vec_entries_expires
                            ON vector_memory.entries(expires_at);
                        CREATE INDEX IF NOT EXISTS idx_vec_entries_type
                            ON vector_memory.entries(memory_type);
                    """)
                conn.commit()
                PostgreSQLVectorBackend._schema_initialized = True
            finally:
                self._put_conn(conn)

    # -- VectorBackend protocol implementation --------------------------------

    def store_vector(
        self,
        key: MemoryKey,
        text: str,
        embedding: list[float],
        metadata: dict[str, Any],
        memory_type: str,
        expires_at: datetime | None = None,
    ) -> None:
        """Store a vector with metadata."""
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO vector_memory.entries
                        (user_id, namespace, key, text, embedding, metadata,
                         memory_type, expires_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (user_id, namespace, key) DO UPDATE SET
                        text = EXCLUDED.text,
                        embedding = EXCLUDED.embedding,
                        metadata = EXCLUDED.metadata,
                        memory_type = EXCLUDED.memory_type,
                        updated_at = NOW(),
                        expires_at = EXCLUDED.expires_at
                    """,
                    (
                        self._user_id,
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
        finally:
            self._put_conn(conn)

    def get_vector(self, key: MemoryKey) -> VectorEntry | None:
        """Get a single vector by key."""
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM vector_memory.entries "
                    "WHERE user_id = %s AND namespace = %s AND key = %s",
                    (self._user_id, key.namespace, key.key),
                )
                row = cur.fetchone()
        finally:
            self._put_conn(conn)
        if row is None:
            return None
        return self._row_to_entry(row)

    def search_vectors(
        self,
        query_embedding: list[float],
        namespace: str | None,
        top_k: int,
        threshold: float | None,
        filters: list[MetadataFilter] | None,
    ) -> list[VectorEntry]:
        """Search vectors by cosine similarity (computed in Python)."""
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                sql = "SELECT * FROM vector_memory.entries WHERE user_id = %s"
                params: list[Any] = [self._user_id]

                if namespace:
                    sql += " AND namespace = %s"
                    params.append(namespace)

                # Exclude expired entries
                sql += " AND (expires_at IS NULL OR expires_at > NOW())"

                cur.execute(sql, params)
                rows = cur.fetchall()
        finally:
            self._put_conn(conn)

        # Compute cosine similarity in Python; apply threshold + metadata filters.
        results: list[VectorEntry] = []
        for row in rows:
            emb = row["embedding"]
            if isinstance(emb, str):
                emb = json.loads(emb)
            score = _cosine_similarity(query_embedding, emb)
            if threshold is not None and score < threshold:
                continue

            meta_raw = row["metadata"]
            metadata: dict[str, Any] = (
                json.loads(meta_raw) if isinstance(meta_raw, str) else (meta_raw or {})
            )
            if filters and not match_metadata_filters(filters, metadata):
                continue

            entry = self._row_to_entry(row)
            entry.score = score
            entry.final_score = score
            results.append(entry)

        # Sort by score descending, limit
        results.sort(key=lambda e: e.score, reverse=True)
        return results[:top_k]

    def delete_vector(self, key: MemoryKey) -> bool:
        """Delete a vector. Returns True if deleted."""
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM vector_memory.entries "
                    "WHERE user_id = %s AND namespace = %s AND key = %s",
                    (self._user_id, key.namespace, key.key),
                )
            conn.commit()
            return cur.rowcount > 0
        finally:
            self._put_conn(conn)

    def list_keys(self, namespace: str | None = None) -> list[MemoryKey]:
        """List all keys."""
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                if namespace:
                    cur.execute(
                        "SELECT namespace, key FROM vector_memory.entries "
                        "WHERE user_id = %s AND namespace = %s ORDER BY key",
                        (self._user_id, namespace),
                    )
                else:
                    cur.execute(
                        "SELECT namespace, key FROM vector_memory.entries "
                        "WHERE user_id = %s ORDER BY namespace, key",
                        (self._user_id,),
                    )
                return [
                    MemoryKey(namespace=r["namespace"], key=r["key"])
                    for r in cur.fetchall()
                ]
        finally:
            self._put_conn(conn)

    def clear_namespace(self, namespace: str) -> int:
        """Clear all vectors in a namespace."""
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM vector_memory.entries "
                    "WHERE user_id = %s AND namespace = %s",
                    (self._user_id, namespace),
                )
            conn.commit()
            return cur.rowcount
        finally:
            self._put_conn(conn)

    def get_stats(self) -> dict[str, Any]:
        """Get vector memory stats."""
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) as total, "
                    "COUNT(DISTINCT namespace) as namespaces "
                    "FROM vector_memory.entries WHERE user_id = %s",
                    (self._user_id,),
                )
                row = cur.fetchone()
                return {
                    "total_vectors": row["total"],
                    "namespaces": row["namespaces"],
                    "backend": "postgresql",
                }
        finally:
            self._put_conn(conn)

    def touch_vector(self, key: MemoryKey) -> None:
        """Update the accessed_at timestamp."""
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE vector_memory.entries SET accessed_at = NOW() "
                    "WHERE user_id = %s AND namespace = %s AND key = %s",
                    (self._user_id, key.namespace, key.key),
                )
            conn.commit()
        finally:
            self._put_conn(conn)

    def list_by_type(
        self,
        memory_type: str,
        older_than: datetime | None = None,
        limit: int = 100,
    ) -> list[VectorEntry]:
        """List entries of a given type, optionally filtered by age."""
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                sql = (
                    "SELECT * FROM vector_memory.entries "
                    "WHERE user_id = %s AND memory_type = %s"
                )
                params: list[Any] = [self._user_id, memory_type]
                if older_than is not None:
                    sql += " AND created_at < %s"
                    params.append(older_than)
                sql += " ORDER BY created_at ASC LIMIT %s"
                params.append(limit)
                cur.execute(sql, params)
                return [self._row_to_entry(r) for r in cur.fetchall()]
        finally:
            self._put_conn(conn)

    def purge_expired(self) -> int:
        """Remove expired vectors. Returns count deleted."""
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM vector_memory.entries "
                    "WHERE user_id = %s AND expires_at IS NOT NULL AND expires_at < NOW()",
                    (self._user_id,),
                )
            conn.commit()
            return cur.rowcount
        finally:
            self._put_conn(conn)

    def close(self) -> None:
        """No-op — pool lifecycle managed by PGPoolManager."""

    # -- internal ------------------------------------------------------------

    @staticmethod
    def _row_to_entry(row: Any) -> VectorEntry:
        emb = row["embedding"]
        if isinstance(emb, str):
            emb = json.loads(emb)

        meta = row["metadata"]
        if isinstance(meta, str):
            meta = json.loads(meta)

        return VectorEntry(
            key=MemoryKey(namespace=row["namespace"], key=row["key"]),
            text=row["text"],
            embedding=emb,
            metadata=meta or {},
            memory_type=row["memory_type"],
            created_at=row["created_at"],
            updated_at=row.get("updated_at"),
            accessed_at=row.get("accessed_at"),
        )
