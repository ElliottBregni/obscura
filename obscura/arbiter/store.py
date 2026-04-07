"""obscura.arbiter.store — SQLite persistence for Arbiter verdicts.

Follows the same pattern as ``obscura.eval.store``. Verdicts are
append-only and queryable for score trends and failure analysis.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from obscura.arbiter.types import ArbiterEvent


def _db_path() -> Path:
    return Path.home() / ".obscura" / "arbiter.db"


def _open() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS verdicts (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            kind          TEXT NOT NULL,
            verdict       TEXT NOT NULL,
            target_id     TEXT NOT NULL DEFAULT '',
            session_id    TEXT NOT NULL DEFAULT '',
            run_id        TEXT NOT NULL DEFAULT '',
            det_score     REAL NOT NULL DEFAULT 0,
            judge_score   REAL,
            composite     REAL NOT NULL DEFAULT 0,
            feedback      TEXT NOT NULL DEFAULT '',
            details       TEXT NOT NULL DEFAULT '[]',
            retry_count   INTEGER NOT NULL DEFAULT 0,
            metadata      TEXT NOT NULL DEFAULT '{}',
            created_at    REAL NOT NULL
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_verdicts_session
        ON verdicts (session_id, created_at)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_verdicts_target
        ON verdicts (target_id, created_at)
    """)
    conn.commit()


class ArbiterStore:
    """Append-only verdict store for audit and trend analysis."""

    def record(self, event: ArbiterEvent) -> None:
        """Persist a single Arbiter event."""
        conn = _open()
        try:
            conn.execute(
                """INSERT INTO verdicts
                   (kind, verdict, target_id, session_id, run_id,
                    det_score, judge_score, composite, feedback,
                    details, retry_count, metadata, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    event.kind.value,
                    event.verdict.value,
                    event.target_id,
                    event.session_id,
                    event.run_id,
                    event.score.deterministic,
                    event.score.judge,
                    event.score.composite,
                    event.score.feedback,
                    json.dumps(list(event.score.details)),
                    event.retry_count,
                    json.dumps(event.metadata),
                    time.time(),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def recent(
        self,
        *,
        session_id: str = "",
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Return recent verdicts, newest first."""
        conn = _open()
        try:
            if session_id:
                rows = conn.execute(
                    "SELECT * FROM verdicts WHERE session_id = ? ORDER BY created_at DESC LIMIT ?",
                    (session_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM verdicts ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def stats(self, *, session_id: str = "") -> dict[str, Any]:
        """Aggregate verdict stats."""
        conn = _open()
        try:
            where = "WHERE session_id = ?" if session_id else ""
            params: tuple[Any, ...] = (session_id,) if session_id else ()

            total = conn.execute(
                f"SELECT COUNT(*) as cnt FROM verdicts {where}", params
            ).fetchone()["cnt"]

            by_verdict = conn.execute(
                f"SELECT verdict, COUNT(*) as cnt FROM verdicts {where} GROUP BY verdict",
                params,
            ).fetchall()

            avg_score = conn.execute(
                f"SELECT AVG(composite) as avg FROM verdicts {where}", params
            ).fetchone()["avg"]

            return {
                "total": total,
                "by_verdict": {r["verdict"]: r["cnt"] for r in by_verdict},
                "avg_composite_score": round(avg_score, 3) if avg_score else 0.0,
            }
        finally:
            conn.close()
