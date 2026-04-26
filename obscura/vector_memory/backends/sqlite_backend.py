"""SQLite-based vector memory backend (extracted from original implementation)."""

from __future__ import annotations

import hashlib
import json
import logging
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

_logger = logging.getLogger(__name__)


def _sqlite_supports_json_patch() -> bool:
    """Probe at module load: does the bundled sqlite3 expose json_patch()?"""
    try:
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute("SELECT json_patch('{\"a\":1}', '{\"b\":2}')").fetchone()
            return True
        finally:
            conn.close()
    except sqlite3.OperationalError:
        return False


_JSON_PATCH_AVAILABLE = _sqlite_supports_json_patch()


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

    def __init__(
        self,
        config: BackendConfig,
        db_path: Path | None = None,
        decay_config: Any | None = None,
    ) -> None:
        """Initialize SQLite backend."""
        self.config = config
        self._decay_config = decay_config
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

        # Schema migration: add accessed_at column (idempotent)
        try:
            conn.execute("ALTER TABLE vector_memory ADD COLUMN accessed_at TIMESTAMP")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_vec_memory_accessed ON vector_memory(accessed_at)",
            )
            conn.commit()
        except sqlite3.OperationalError:
            # Column already exists — expected on subsequent runs
            pass

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
            accessed_at=datetime.fromisoformat(row["accessed_at"])
            if row["accessed_at"]
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

            created_at = datetime.fromisoformat(row["created_at"])
            updated_at = (
                datetime.fromisoformat(row["updated_at"]) if row["updated_at"] else None
            )
            accessed_at_raw = row["accessed_at"]
            accessed_at = (
                datetime.fromisoformat(accessed_at_raw) if accessed_at_raw else None
            )

            # Per-type decay via centralized compute_decay
            from obscura.vector_memory.decay import compute_decay as _compute_decay

            if self._decay_config is not None:
                decay = _compute_decay(
                    row["memory_type"],
                    created_at,
                    accessed_at,
                    self._decay_config,
                )
            elif getattr(self.config, "decay_half_life_seconds", None):
                # Legacy single half-life fallback
                half_life = self.config.decay_half_life_seconds or (30 * 86400)
                age_s = (datetime.now(UTC) - created_at).total_seconds()
                decay = 0.5 ** (age_s / half_life) if half_life > 0 else 1.0
            else:
                decay = 1.0

            entry = VectorEntry(
                key=MemoryKey(namespace=row["namespace"], key=row["key"]),
                text=row["text"],
                embedding=embedding,
                metadata=metadata,
                memory_type=row["memory_type"],
                created_at=created_at,
                updated_at=updated_at,
                accessed_at=accessed_at,
                score=similarity,
                rerank_score=decay,
                final_score=similarity * decay,
            )
            results.append(entry)

        results.sort(key=lambda x: x.final_score, reverse=True)
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

    def touch_vector(self, key: MemoryKey) -> None:
        """Update ``accessed_at`` to now.  No-op if key doesn't exist."""
        conn = self._get_conn()
        conn.execute(
            "UPDATE vector_memory SET accessed_at = ? WHERE namespace = ? AND key = ?",
            (datetime.now(UTC).isoformat(), key.namespace, key.key),
        )
        conn.commit()

    def update_metadata(self, key: MemoryKey, partial: dict[str, Any]) -> None:
        """Atomic merge of ``partial`` into the metadata JSON column.

        ``accessed_at`` is split out because it lives in its own column.
        Everything else is folded into the metadata JSON dict.  No-op if
        the key doesn't exist.
        """
        if not partial:
            return
        accessed_at = partial.get("accessed_at")
        md_updates = {k: v for k, v in partial.items() if k != "accessed_at"}
        conn = self._get_conn()
        if md_updates and _JSON_PATCH_AVAILABLE:
            patch = json.dumps(md_updates)
            if accessed_at is not None:
                conn.execute(
                    "UPDATE vector_memory SET "
                    "metadata = json_patch(COALESCE(metadata, '{}'), ?), "
                    "accessed_at = ? "
                    "WHERE namespace = ? AND key = ?",
                    (patch, accessed_at, key.namespace, key.key),
                )
            else:
                conn.execute(
                    "UPDATE vector_memory SET "
                    "metadata = json_patch(COALESCE(metadata, '{}'), ?) "
                    "WHERE namespace = ? AND key = ?",
                    (patch, key.namespace, key.key),
                )
            conn.commit()
            return

        if md_updates:
            row = conn.execute(
                "SELECT metadata FROM vector_memory WHERE namespace = ? AND key = ?",
                (key.namespace, key.key),
            ).fetchone()
            if row is None:
                return
            current_md = json.loads(row["metadata"]) if row["metadata"] else {}
            new_md = {**current_md, **md_updates}
            if accessed_at is not None:
                conn.execute(
                    "UPDATE vector_memory SET metadata = ?, accessed_at = ? "
                    "WHERE namespace = ? AND key = ?",
                    (json.dumps(new_md), accessed_at, key.namespace, key.key),
                )
            else:
                conn.execute(
                    "UPDATE vector_memory SET metadata = ? "
                    "WHERE namespace = ? AND key = ?",
                    (json.dumps(new_md), key.namespace, key.key),
                )
            conn.commit()
            return

        if accessed_at is not None:
            conn.execute(
                "UPDATE vector_memory SET accessed_at = ? "
                "WHERE namespace = ? AND key = ?",
                (accessed_at, key.namespace, key.key),
            )
            conn.commit()

    def list_by_type(
        self,
        memory_type: str,
        older_than: datetime | None = None,
        limit: int = 100,
    ) -> list[VectorEntry]:
        """List entries of a given type, optionally filtered by age."""
        conn = self._get_conn()
        query = "SELECT * FROM vector_memory WHERE memory_type = ?"
        params: list[Any] = [memory_type]
        if older_than is not None:
            query += " AND created_at < ?"
            params.append(older_than.isoformat())
        query += " ORDER BY created_at ASC LIMIT ?"
        params.append(limit)

        rows = conn.execute(query, params).fetchall()
        entries: list[VectorEntry] = []
        for row in rows:
            accessed_at_raw = row["accessed_at"]
            entries.append(
                VectorEntry(
                    key=MemoryKey(namespace=row["namespace"], key=row["key"]),
                    text=row["text"],
                    embedding=json.loads(row["embedding"]),
                    metadata=json.loads(row["metadata"]) if row["metadata"] else {},
                    memory_type=row["memory_type"],
                    created_at=datetime.fromisoformat(row["created_at"]),
                    updated_at=datetime.fromisoformat(row["updated_at"])
                    if row["updated_at"]
                    else None,
                    accessed_at=datetime.fromisoformat(accessed_at_raw)
                    if accessed_at_raw
                    else None,
                ),
            )
        return entries

    def purge_expired(self) -> int:
        """Delete entries whose ``expires_at`` is in the past."""
        conn = self._get_conn()
        cursor = conn.execute(
            "DELETE FROM vector_memory WHERE expires_at IS NOT NULL AND expires_at < ?",
            (datetime.now(UTC).isoformat(),),
        )
        conn.commit()
        return cursor.rowcount

    def close(self) -> None:
        """Close the database connection."""
        if hasattr(self._local, "conn") and self._local.conn:
            self._local.conn.close()
            self._local.conn = None
