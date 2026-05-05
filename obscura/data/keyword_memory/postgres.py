"""PostgreSQL tsvector backend for the keyword-memory repository.

Mirrors :mod:`obscura.data.keyword_memory.sqlite` so swapping
``OBSCURA_DB_URL=postgresql://...`` doesn't require any caller change.
All SQL lives in :data:`_QUERIES`; connections come from
:func:`obscura.data.engine.postgres_connection`.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from obscura.data.engine import postgres_connection
from obscura.data.keyword_memory.protocol import Memory

logger = logging.getLogger(__name__)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS obscura_keyword_memories (
    id          BIGSERIAL PRIMARY KEY,
    namespace   TEXT NOT NULL DEFAULT 'default',
    content     TEXT NOT NULL,
    metadata    JSONB NOT NULL DEFAULT '{}'::jsonb,
    content_tsv TSVECTOR GENERATED ALWAYS AS
                    (to_tsvector('english', content)) STORED,
    created_at  DOUBLE PRECISION NOT NULL,
    updated_at  DOUBLE PRECISION NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_obscura_kw_mem_tsv
    ON obscura_keyword_memories USING GIN (content_tsv);

CREATE INDEX IF NOT EXISTS idx_obscura_kw_mem_ns
    ON obscura_keyword_memories (namespace, created_at DESC);
"""


_QUERIES = {
    "insert": (
        "INSERT INTO obscura_keyword_memories (namespace, content, metadata,"
        " created_at, updated_at) VALUES (%s, %s, %s::jsonb, %s, %s)"
        " RETURNING id"
    ),
    "search_with_ns": (
        "SELECT id, namespace, content, metadata, created_at, updated_at,"
        " ts_rank_cd(content_tsv, plainto_tsquery('english', %s)) AS rank"
        " FROM obscura_keyword_memories"
        " WHERE content_tsv @@ plainto_tsquery('english', %s)"
        " AND namespace = %s ORDER BY rank DESC LIMIT %s"
    ),
    "search_all": (
        "SELECT id, namespace, content, metadata, created_at, updated_at,"
        " ts_rank_cd(content_tsv, plainto_tsquery('english', %s)) AS rank"
        " FROM obscura_keyword_memories"
        " WHERE content_tsv @@ plainto_tsquery('english', %s)"
        " ORDER BY rank DESC LIMIT %s"
    ),
    "delete_by_id": "DELETE FROM obscura_keyword_memories WHERE id = %s",
    "list_namespaces": (
        "SELECT namespace, COUNT(*) FROM obscura_keyword_memories"
        " GROUP BY namespace ORDER BY namespace"
    ),
    "list_by_prefix": (
        "SELECT id, namespace, content, metadata, created_at, updated_at"
        " FROM obscura_keyword_memories"
        " WHERE namespace = %s OR namespace LIKE %s"
        " ORDER BY updated_at DESC LIMIT %s"
    ),
    "count_total": "SELECT COUNT(*) FROM obscura_keyword_memories",
}


class PostgresKeywordMemoryRepo:
    """Postgres implementation of :class:`KeywordMemoryRepo`."""

    _schema_initialized = False

    def __init__(self) -> None:
        if PostgresKeywordMemoryRepo._schema_initialized:
            return
        with postgres_connection() as conn:
            try:
                with conn.cursor() as cur:
                    cur.execute(_SCHEMA)
                conn.commit()
                PostgresKeywordMemoryRepo._schema_initialized = True
            except Exception:
                conn.rollback()
                raise

    def remember(
        self,
        content: str,
        *,
        namespace: str = "default",
        metadata: dict[str, Any] | None = None,
    ) -> int:
        if not content or not content.strip():
            msg = "memory content must be non-empty"
            raise ValueError(msg)
        now = time.time()
        meta_json = json.dumps(metadata or {}, ensure_ascii=False)
        with postgres_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    _QUERIES["insert"],
                    (namespace, content.strip(), meta_json, now, now),
                )
                row = cur.fetchone()
            conn.commit()
        return int(row[0]) if row else 0

    def recall(
        self,
        query: str,
        *,
        namespace: str | None = None,
        top_k: int = 5,
    ) -> list[Memory]:
        if not query or not query.strip():
            return []
        with postgres_connection() as conn:
            try:
                with conn.cursor() as cur:
                    if namespace is None:
                        cur.execute(
                            _QUERIES["search_all"],
                            (query.strip(), query.strip(), int(top_k)),
                        )
                    else:
                        cur.execute(
                            _QUERIES["search_with_ns"],
                            (
                                query.strip(),
                                query.strip(),
                                namespace,
                                int(top_k),
                            ),
                        )
                    rows = cur.fetchall()
            except Exception:
                logger.debug("postgres tsvector query failed", exc_info=True)
                return []
        return [_row_to_memory(r, has_rank=True) for r in rows]

    def forget(self, memory_id: int) -> bool:
        with postgres_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(_QUERIES["delete_by_id"], (int(memory_id),))
                removed = cur.rowcount > 0
            conn.commit()
        return removed

    def list_namespaces(self) -> list[tuple[str, int]]:
        with postgres_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(_QUERIES["list_namespaces"])
                rows = cur.fetchall()
        return [(r[0], int(r[1])) for r in rows]

    def list_by_namespace_prefix(
        self,
        prefix: str,
        *,
        limit: int = 50,
    ) -> list[Memory]:
        if not prefix:
            return []
        with postgres_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    _QUERIES["list_by_prefix"],
                    (prefix, f"{prefix}:%", int(limit)),
                )
                rows = cur.fetchall()
        return [_row_to_memory(r, has_rank=False) for r in rows]

    def stats(self) -> dict[str, Any]:
        with postgres_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(_QUERIES["count_total"])
                total_row = cur.fetchone()
        return {
            "backend": "postgres",
            "total": int(total_row[0]) if total_row else 0,
            "namespaces": dict(self.list_namespaces()),
        }

    def close(self) -> None:
        """No-op: connections come from a pool checkout/return per call."""


def _row_to_memory(row: Any, *, has_rank: bool) -> Memory:  # noqa: ANN401  # psycopg2 row tuple
    raw_meta = row[3]
    parsed: Any
    if isinstance(raw_meta, str):
        try:
            parsed = json.loads(raw_meta)
        except (json.JSONDecodeError, TypeError):
            logger.debug("invalid metadata json: %r", raw_meta, exc_info=True)
            parsed = {}
    elif isinstance(raw_meta, dict):
        parsed = raw_meta
    else:
        parsed = {}
    metadata = (
        {str(k): v for k, v in parsed.items()} if isinstance(parsed, dict) else {}
    )
    score = float(row[6]) if has_rank and len(row) > 6 else 0.0
    return Memory(
        id=int(row[0]),
        namespace=str(row[1]),
        content=str(row[2]),
        metadata=metadata,
        created_at=float(row[4]),
        updated_at=float(row[5]),
        score=score,
    )
