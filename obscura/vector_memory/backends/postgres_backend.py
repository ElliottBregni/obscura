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
from typing import Any

from obscura.core.pg_config import PGPoolManager
from obscura.vector_memory.backends.base import BackendConfig, VectorEntry

logger = logging.getLogger(__name__)


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
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
        key: Any,
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

    def get_vector(self, key: Any) -> VectorEntry | None:
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
        *,
        namespace: str | None = None,
        limit: int = 10,
        min_score: float = 0.0,
        memory_type: str | None = None,
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
                if memory_type:
                    sql += " AND memory_type = %s"
                    params.append(memory_type)

                # Exclude expired entries
                sql += " AND (expires_at IS NULL OR expires_at > NOW())"

                cur.execute(sql, params)
                rows = cur.fetchall()
        finally:
            self._put_conn(conn)

        # Compute cosine similarity in Python
        results: list[VectorEntry] = []
        for row in rows:
            emb = row["embedding"]
            if isinstance(emb, str):
                emb = json.loads(emb)
            score = _cosine_similarity(query_embedding, emb)
            if score >= min_score:
                entry = self._row_to_entry(row)
                entry.score = score
                entry.final_score = score
                results.append(entry)

        # Sort by score descending, limit
        results.sort(key=lambda e: e.score, reverse=True)
        return results[:limit]

    def delete_vector(self, key: Any) -> bool:
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

    def list_keys(self, namespace: str | None = None) -> list[Any]:
        """List all keys."""
        from obscura.memory import MemoryKey

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

    def touch_vector(self, key: Any) -> None:
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

    def update_metadata(self, key: Any, partial: dict[str, Any]) -> None:
        """Atomic merge of ``partial`` into the metadata JSONB column.

        ``accessed_at`` is split out because it lives in its own column.
        Everything else is merged into the metadata JSONB via the ``||``
        operator (shallow merge, last-write-wins per key).  No-op if the
        key doesn't exist.
        """
        if not partial:
            return
        accessed_at = partial.get("accessed_at")
        md_updates = {k: v for k, v in partial.items() if k != "accessed_at"}

        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                if md_updates and accessed_at is not None:
                    cur.execute(
                        "UPDATE vector_memory.entries SET "
                        "metadata = COALESCE(metadata, '{}'::jsonb) || %s::jsonb, "
                        "accessed_at = %s "
                        "WHERE user_id = %s AND namespace = %s AND key = %s",
                        (
                            json.dumps(md_updates),
                            accessed_at,
                            self._user_id,
                            key.namespace,
                            key.key,
                        ),
                    )
                elif md_updates:
                    cur.execute(
                        "UPDATE vector_memory.entries SET "
                        "metadata = COALESCE(metadata, '{}'::jsonb) || %s::jsonb "
                        "WHERE user_id = %s AND namespace = %s AND key = %s",
                        (
                            json.dumps(md_updates),
                            self._user_id,
                            key.namespace,
                            key.key,
                        ),
                    )
                elif accessed_at is not None:
                    cur.execute(
                        "UPDATE vector_memory.entries SET accessed_at = %s "
                        "WHERE user_id = %s AND namespace = %s AND key = %s",
                        (accessed_at, self._user_id, key.namespace, key.key),
                    )
            conn.commit()
        finally:
            self._put_conn(conn)

    def list_by_type(
        self,
        memory_type: str,
        namespace: str | None = None,
    ) -> list[VectorEntry]:
        """List vectors by type."""
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                sql = (
                    "SELECT * FROM vector_memory.entries "
                    "WHERE user_id = %s AND memory_type = %s"
                )
                params: list[Any] = [self._user_id, memory_type]
                if namespace:
                    sql += " AND namespace = %s"
                    params.append(namespace)
                sql += " ORDER BY created_at DESC"
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
        from obscura.memory import MemoryKey

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
