"""obscura.core.supervisor.memory_gate — Memory commit gating with deduplication.

Controls what gets written to memory during COMMITTING_MEMORY phase.
Prevents duplicate writes, enforces importance thresholds, and ensures
memory stability across runs.
"""

from __future__ import annotations

import hashlib
import logging
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from obscura.core.supervisor.db_backend import (
    DatabaseBackend,
    SQLiteSupervisorBackend,
    translate_sql,
)
from obscura.core.supervisor.types import (
    MemoryCandidate,
    MemoryCommitResult,
    SupervisorEvent,
    SupervisorEventKind,
)

logger = logging.getLogger(__name__)

# Scoring weights
IMPORTANCE_WEIGHT = 0.4
RECENCY_WEIGHT = 0.3
RELEVANCE_WEIGHT = 0.3

# Recency decay half-life in hours
RECENCY_HALF_LIFE_HOURS = 24.0


class MemoryCommitGate:
    """Gates memory commits for a supervisor run.

    Prevents:
    - Duplicate writes (content hash dedup)
    - Low-quality writes (importance threshold)
    - Unbounded writes (batch size limit)

    Usage::

        gate = MemoryCommitGate(
            db_path="/tmp/supervisor.db",
            session_id="sess-1",
            run_id="run-abc",
        )

        # Queue candidates during the run
        gate.queue(MemoryCandidate(key="fact-1", content="...", ...))
        gate.queue(MemoryCandidate(key="fact-2", content="...", ...))

        # Commit during COMMITTING_MEMORY phase
        result = await gate.commit()
        # result.committed = 2, result.deduplicated = 0, result.gated = 0
    """

    def __init__(
        self,
        db_path: str | Path | None = None,
        session_id: str = "",
        run_id: str = "",
        *,
        min_importance: float = 0.3,
        max_batch_size: int = 20,
        backend: DatabaseBackend | None = None,
    ) -> None:
        if backend is not None:
            self._backend = backend
        elif db_path is not None:
            self._backend = SQLiteSupervisorBackend(db_path)
        else:
            msg = "Either db_path or backend must be provided"
            raise ValueError(msg)

        self._session_id = session_id
        self._run_id = run_id
        self._min_importance = min_importance
        self._max_batch_size = max_batch_size

        self._queue: list[MemoryCandidate] = []
        self._events: list[SupervisorEvent] = []

    def _sql(self, sql: str) -> str:
        """Translate SQL for the current dialect."""
        return translate_sql(sql, self._backend.dialect)

    # -- queuing -------------------------------------------------------------

    def queue(self, candidate: MemoryCandidate) -> None:
        """Queue a memory candidate for commit gating."""
        self._queue.append(candidate)

    def queue_item(
        self,
        key: str,
        content: str,
        *,
        importance: float = 0.5,
        relevance: float = 0.0,
        pinned: bool = False,
    ) -> None:
        """Queue a memory item (convenience method)."""
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        self.queue(
            MemoryCandidate(
                key=key,
                content=content,
                content_hash=content_hash,
                importance=importance,
                relevance=relevance,
                pinned=pinned,
                source_run_id=self._run_id,
            ),
        )

    @property
    def queue_size(self) -> int:
        return len(self._queue)

    @property
    def events(self) -> list[SupervisorEvent]:
        return list(self._events)

    # -- commit --------------------------------------------------------------

    def commit_sync(self) -> MemoryCommitResult:
        """Execute the commit gate (sync, typically run in asyncio.to_thread).

        Steps:
        1. Deduplicate by content_hash (against existing + within batch)
        2. Filter by importance threshold (unless pinned)
        3. Limit batch size
        4. Write to memory_commits table
        5. Emit events

        Returns:
            MemoryCommitResult with counts.

        """
        committed = 0
        deduplicated = 0
        gated = 0
        errors = 0

        conn = self._backend.get_conn()
        try:
            # Get existing content hashes for this session
            existing_rows = conn.execute(
                self._sql(
                    "SELECT content_hash FROM memory_commits WHERE session_id = ?"
                ),
                (self._session_id,),
            ).fetchall()
            existing_hashes: set[str] = {row["content_hash"] for row in existing_rows}

            # Dedupe within batch
            seen_hashes: set[str] = set()
            candidates: list[MemoryCandidate] = []

            for item in self._queue:
                if item.content_hash in existing_hashes:
                    deduplicated += 1
                    self._emit_event(
                        SupervisorEventKind.MEMORY_DEDUPLICATED,
                        {"key": item.key, "content_hash": item.content_hash[:12]},
                    )
                    continue

                if item.content_hash in seen_hashes:
                    deduplicated += 1
                    continue

                # Gate by importance (pinned items bypass)
                if not item.pinned and item.importance < self._min_importance:
                    gated += 1
                    self._emit_event(
                        SupervisorEventKind.MEMORY_GATED,
                        {
                            "key": item.key,
                            "importance": item.importance,
                            "threshold": self._min_importance,
                        },
                    )
                    continue

                seen_hashes.add(item.content_hash)
                candidates.append(item)

            # Batch size limit (keep highest-scored items)
            if len(candidates) > self._max_batch_size:
                candidates.sort(key=lambda c: c.score, reverse=True)
                gated += len(candidates) - self._max_batch_size
                candidates = candidates[: self._max_batch_size]

            # Write to DB
            now = datetime.now(UTC).isoformat()
            for item in candidates:
                try:
                    conn.execute(
                        self._sql(
                            "INSERT OR IGNORE INTO memory_commits "
                            "(session_id, run_id, key, content_hash, importance, "
                            " pinned, committed_at) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?)"
                        ),
                        (
                            self._session_id,
                            self._run_id,
                            item.key,
                            item.content_hash,
                            item.importance,
                            1 if item.pinned else 0,
                            now,
                        ),
                    )
                    committed += 1
                    self._emit_event(
                        SupervisorEventKind.MEMORY_COMMIT,
                        {
                            "key": item.key,
                            "content_hash": item.content_hash[:12],
                            "importance": item.importance,
                            "pinned": item.pinned,
                        },
                    )
                except Exception:
                    errors += 1
                    logger.exception("Failed to commit memory item: %s", item.key)

            conn.commit()
        finally:
            self._backend.put_conn(conn)

        result = MemoryCommitResult(
            committed=committed,
            deduplicated=deduplicated,
            gated=gated,
            errors=errors,
        )

        # Warn explicitly when all candidates were below the importance threshold.
        # Silent zero-commit is hard to diagnose without this signal.
        if committed == 0 and gated > 0 and len(self._queue) > 0:
            logger.warning(
                "MemoryCommitGate: all %d candidate(s) were below importance "
                "threshold %.2f — nothing committed to session %s",
                gated,
                self._min_importance,
                self._session_id,
            )

        logger.debug(
            "Memory commit: %d committed, %d deduped, %d gated, %d errors",
            committed,
            deduplicated,
            gated,
            errors,
        )
        return result

    # -- retrieval helpers ---------------------------------------------------

    def get_committed_hashes(self) -> set[str]:
        """Get all content hashes committed for this session (sync)."""
        conn = self._backend.get_conn()
        try:
            rows = conn.execute(
                self._sql(
                    "SELECT content_hash FROM memory_commits WHERE session_id = ?"
                ),
                (self._session_id,),
            ).fetchall()
        finally:
            self._backend.put_conn(conn)
        return {row["content_hash"] for row in rows}

    def get_commits_for_run(self) -> list[dict[str, Any]]:
        """Get all commits for the current run (sync)."""
        conn = self._backend.get_conn()
        try:
            rows = conn.execute(
                self._sql(
                    "SELECT key, content_hash, importance, pinned, committed_at "
                    "FROM memory_commits WHERE run_id = ? ORDER BY committed_at"
                ),
                (self._run_id,),
            ).fetchall()
        finally:
            self._backend.put_conn(conn)
        return [dict(row) for row in rows]

    # -- internal ------------------------------------------------------------

    def _emit_event(
        self,
        kind: SupervisorEventKind,
        payload: dict[str, Any],
    ) -> None:
        self._events.append(
            SupervisorEvent(
                kind=kind,
                run_id=self._run_id,
                session_id=self._session_id,
                payload=payload,
            ),
        )

    def close(self) -> None:
        self._backend.close()


# ---------------------------------------------------------------------------
# Scoring helpers
# ---------------------------------------------------------------------------


def recency_decay(age_hours: float) -> float:
    """Exponential recency decay. Returns value in [0, 1]."""
    if age_hours <= 0:
        return 1.0
    return math.exp(-age_hours / RECENCY_HALF_LIFE_HOURS)


def compute_memory_score(
    importance: float,
    relevance: float,
    age_hours: float,
) -> float:
    """Compute composite memory score."""
    recency = recency_decay(age_hours)
    return (
        importance * IMPORTANCE_WEIGHT
        + recency * RECENCY_WEIGHT
        + relevance * RELEVANCE_WEIGHT
    )


def content_hash(content: str) -> str:
    """SHA-256 hash of content for deduplication."""
    return hashlib.sha256(content.encode()).hexdigest()
