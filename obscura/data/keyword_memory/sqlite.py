"""SQLite FTS5 backend for the keyword-memory repository.

Hand-written SQL only. All queries live in :data:`_QUERIES` so a future
schema change touches one place. Connection management is the
:func:`obscura.data.engine.sqlite_connection` context manager — this
class holds no long-lived connection.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from obscura.data.engine import sqlite_connection, sqlite_path
from obscura.data.keyword_memory.protocol import Memory

logger = logging.getLogger(__name__)


_STORE_NAME = "memories"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    namespace   TEXT NOT NULL DEFAULT 'default',
    content     TEXT NOT NULL,
    metadata    TEXT NOT NULL DEFAULT '{}',
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_memories_namespace
    ON memories(namespace, created_at DESC);

CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    content,
    content='memories',
    content_rowid='id',
    tokenize='porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content);
END;

CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content)
        VALUES('delete', old.id, old.content);
END;

CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content)
        VALUES('delete', old.id, old.content);
    INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content);
END;
"""


_QUERIES = {
    "insert": (
        "INSERT INTO memories (namespace, content, metadata, created_at,"
        " updated_at) VALUES (?, ?, ?, ?, ?)"
    ),
    "search_with_ns": (
        "SELECT m.id, m.namespace, m.content, m.metadata, m.created_at,"
        " m.updated_at, bm25(memories_fts) AS rank FROM memories_fts"
        " JOIN memories m ON m.id = memories_fts.rowid"
        " WHERE memories_fts MATCH ? AND m.namespace = ?"
        " ORDER BY rank LIMIT ?"
    ),
    "search_all": (
        "SELECT m.id, m.namespace, m.content, m.metadata, m.created_at,"
        " m.updated_at, bm25(memories_fts) AS rank FROM memories_fts"
        " JOIN memories m ON m.id = memories_fts.rowid"
        " WHERE memories_fts MATCH ? ORDER BY rank LIMIT ?"
    ),
    "delete_by_id": "DELETE FROM memories WHERE id = ?",
    "list_namespaces": (
        "SELECT namespace, COUNT(*) AS n FROM memories GROUP BY namespace"
        " ORDER BY namespace"
    ),
    "list_by_prefix": (
        "SELECT id, namespace, content, metadata, created_at, updated_at"
        " FROM memories WHERE namespace = ? OR namespace LIKE ?"
        " ORDER BY updated_at DESC LIMIT ?"
    ),
    "count_total": "SELECT COUNT(*) AS n FROM memories",
}


class SqliteKeywordMemoryRepo:
    """SQLite implementation of :class:`KeywordMemoryRepo`."""

    _schema_initialized = False

    def __init__(self) -> None:
        if not SqliteKeywordMemoryRepo._schema_initialized:
            with sqlite_connection(_STORE_NAME) as conn:
                conn.executescript(_SCHEMA)
                conn.commit()
            SqliteKeywordMemoryRepo._schema_initialized = True

    @property
    def db_path(self) -> Any:  # noqa: ANN401  # pathlib.Path; Any to keep callers loose
        """Surface the path for diagnostics; not part of the Protocol."""
        return sqlite_path(_STORE_NAME)

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
        with sqlite_connection(_STORE_NAME) as conn:
            cur = conn.execute(
                _QUERIES["insert"],
                (namespace, content.strip(), meta_json, now, now),
            )
            conn.commit()
            return int(cur.lastrowid or 0)

    def recall(
        self,
        query: str,
        *,
        namespace: str | None = None,
        top_k: int = 5,
    ) -> list[Memory]:
        if not query or not query.strip():
            return []
        with sqlite_connection(_STORE_NAME) as conn:
            try:
                if namespace is None:
                    rows = conn.execute(
                        _QUERIES["search_all"],
                        (query.strip(), int(top_k)),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        _QUERIES["search_with_ns"],
                        (query.strip(), namespace, int(top_k)),
                    ).fetchall()
            except Exception:
                logger.debug("fts5 query failed for %r", query, exc_info=True)
                return []
        return [_row_to_memory(row, score_field="rank", negate=True) for row in rows]

    def forget(self, memory_id: int) -> bool:
        with sqlite_connection(_STORE_NAME) as conn:
            cur = conn.execute(_QUERIES["delete_by_id"], (int(memory_id),))
            conn.commit()
            return cur.rowcount > 0

    def list_namespaces(self) -> list[tuple[str, int]]:
        with sqlite_connection(_STORE_NAME) as conn:
            rows = conn.execute(_QUERIES["list_namespaces"]).fetchall()
        return [(r["namespace"], int(r["n"])) for r in rows]

    def list_by_namespace_prefix(
        self,
        prefix: str,
        *,
        limit: int = 50,
    ) -> list[Memory]:
        if not prefix:
            return []
        with sqlite_connection(_STORE_NAME) as conn:
            rows = conn.execute(
                _QUERIES["list_by_prefix"],
                (prefix, f"{prefix}:%", int(limit)),
            ).fetchall()
        return [_row_to_memory(row) for row in rows]

    def stats(self) -> dict[str, Any]:
        with sqlite_connection(_STORE_NAME) as conn:
            total_row = conn.execute(_QUERIES["count_total"]).fetchone()
        return {
            "backend": "sqlite",
            "db_path": str(sqlite_path(_STORE_NAME)),
            "total": int(total_row["n"]) if total_row else 0,
            "namespaces": dict(self.list_namespaces()),
        }

    def close(self) -> None:
        """No-op: per-call connections are managed by the engine context."""


def _row_to_memory(
    row: Any,  # noqa: ANN401  # sqlite3.Row, accessed by string key
    *,
    score_field: str | None = None,
    negate: bool = False,
) -> Memory:
    raw_meta = row["metadata"]
    parsed: Any
    try:
        parsed = json.loads(raw_meta)
    except (json.JSONDecodeError, TypeError):
        logger.debug("invalid metadata json: %r", raw_meta, exc_info=True)
        parsed = {}
    metadata = (
        {str(k): v for k, v in parsed.items()} if isinstance(parsed, dict) else {}
    )
    if score_field is not None:
        raw = float(row[score_field])
        # bm25 is non-positive; negate so higher-is-better
        score = -raw if negate else raw
    else:
        score = 0.0
    return Memory(
        id=row["id"],
        namespace=row["namespace"],
        content=row["content"],
        metadata=metadata,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        score=score,
    )
