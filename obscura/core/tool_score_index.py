"""Tool quality score index — aggregates broker audit data into per-tool scores.

Tracks invocation counts, success/error rates, and latency to produce a
composite quality score per tool.  Used by :class:`ToolRouter` to prioritise
high-quality tools and exclude consistently-failing ones.

Scores are maintained in-memory with optional SQLite persistence so they
survive across sessions within the same process.
"""

from __future__ import annotations

import logging
import math
import os
import sqlite3
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from obscura.plugins.broker import BrokerAuditEntry

logger = logging.getLogger(__name__)

# Actions considered "successful" by the scoring system.
_SUCCESS_ACTIONS: frozenset[str] = frozenset({"executed"})


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


# ---------------------------------------------------------------------------
# Score record
# ---------------------------------------------------------------------------


@dataclass
class ToolScore:
    """Aggregate quality metrics for a single tool."""

    name: str
    invocation_count: int = 0
    success_count: int = 0
    error_count: int = 0
    total_latency_ms: float = 0.0
    last_error: str = ""
    last_used: float = 0.0

    # -- Derived properties --------------------------------------------------

    @property
    def success_rate(self) -> float:
        if self.invocation_count == 0:
            return 0.5  # neutral for unknown tools
        return self.success_count / self.invocation_count

    @property
    def error_rate(self) -> float:
        if self.invocation_count == 0:
            return 0.0
        return self.error_count / self.invocation_count

    @property
    def avg_latency_ms(self) -> float:
        if self.invocation_count == 0:
            return 0.0
        return self.total_latency_ms / self.invocation_count

    @property
    def quality_score(self) -> float:
        """Composite 0.0–1.0 quality score.

        Formula:
            0.5 × success_rate
          + 0.2 × latency_bonus  (faster = better, capped at 10 s)
          + 0.2 × recency_bonus  (used recently = better, decays over 24 h)
          + 0.1 × frequency_bonus (more usage = more signal = higher trust)

        Tools with no invocation history return 0.5 (neutral).
        """
        if self.invocation_count == 0:
            return 0.5

        latency_bonus = 1.0 - _clamp(self.avg_latency_ms / 10_000.0, 0.0, 1.0)

        age_seconds = max(time.time() - self.last_used, 0.0)
        recency_bonus = 1.0 - _clamp(age_seconds / 86_400.0, 0.0, 1.0)

        # Logarithmic frequency bonus — more invocations = more confidence.
        freq_bonus = _clamp(math.log2(self.invocation_count + 1) / 10.0, 0.0, 1.0)

        return _clamp(
            0.5 * self.success_rate
            + 0.2 * latency_bonus
            + 0.2 * recency_bonus
            + 0.1 * freq_bonus,
            0.0,
            1.0,
        )


# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------


class ToolScoreIndex:
    """In-memory index of per-tool quality scores.

    Feed broker audit entries via :meth:`record` and query scores via
    :meth:`get_score` / :meth:`ranked`.
    """

    def __init__(self) -> None:
        self._scores: dict[str, ToolScore] = {}

    # -- Recording -----------------------------------------------------------

    def record(self, entry: BrokerAuditEntry) -> None:
        """Update running stats for a tool based on a broker audit entry."""
        score = self._scores.get(entry.tool)
        if score is None:
            score = ToolScore(name=entry.tool)
            self._scores[entry.tool] = score

        score.invocation_count += 1
        score.last_used = entry.timestamp

        if entry.action in _SUCCESS_ACTIONS:
            score.success_count += 1
        elif entry.action in {"error", "timeout"}:
            score.error_count += 1
            score.last_error = entry.error

        if entry.latency_ms > 0:
            score.total_latency_ms += entry.latency_ms

    # -- Querying ------------------------------------------------------------

    def get_score(self, tool_name: str) -> ToolScore:
        """Return the score for *tool_name*, or a neutral default."""
        return self._scores.get(tool_name, ToolScore(name=tool_name))

    def get_scores(self, tool_names: list[str]) -> dict[str, ToolScore]:
        """Bulk lookup — returns a score for every requested name."""
        return {name: self.get_score(name) for name in tool_names}

    def ranked(self, tool_names: list[str]) -> list[str]:
        """Return *tool_names* sorted by quality score descending."""
        scored = [(name, self.get_score(name).quality_score) for name in tool_names]
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return [name for name, _score in scored]

    # -- Introspection -------------------------------------------------------

    @property
    def known_tools(self) -> list[str]:
        """Return names of all tools with at least one recorded invocation."""
        return [name for name, s in self._scores.items() if s.invocation_count > 0]

    def __len__(self) -> int:
        return len(self._scores)

    # -- SQLite persistence --------------------------------------------------

    def save(self, db_path: str) -> None:
        """Persist all scores to a SQLite database."""
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS tool_scores ("
                "  name TEXT PRIMARY KEY,"
                "  invocation_count INTEGER,"
                "  success_count INTEGER,"
                "  error_count INTEGER,"
                "  total_latency_ms REAL,"
                "  last_error TEXT,"
                "  last_used REAL"
                ")",
            )
            for s in self._scores.values():
                conn.execute(
                    "INSERT OR REPLACE INTO tool_scores "
                    "(name, invocation_count, success_count, error_count, "
                    " total_latency_ms, last_error, last_used) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        s.name,
                        s.invocation_count,
                        s.success_count,
                        s.error_count,
                        s.total_latency_ms,
                        s.last_error,
                        s.last_used,
                    ),
                )
            conn.commit()
        finally:
            conn.close()

    def load(self, db_path: str) -> None:
        """Load scores from a SQLite database, merging with any in-memory data."""
        if not os.path.exists(db_path):
            return

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            cursor = conn.execute("SELECT * FROM tool_scores")
            for row in cursor:
                name = row["name"]
                if name not in self._scores:
                    self._scores[name] = ToolScore(
                        name=name,
                        invocation_count=row["invocation_count"],
                        success_count=row["success_count"],
                        error_count=row["error_count"],
                        total_latency_ms=row["total_latency_ms"],
                        last_error=row["last_error"],
                        last_used=row["last_used"],
                    )
                else:
                    # Merge: add persisted counts to in-memory counts
                    s = self._scores[name]
                    s.invocation_count += row["invocation_count"]
                    s.success_count += row["success_count"]
                    s.error_count += row["error_count"]
                    s.total_latency_ms += row["total_latency_ms"]
                    s.last_used = max(s.last_used, row["last_used"])
                    if not s.last_error and row["last_error"]:
                        s.last_error = row["last_error"]
        except sqlite3.OperationalError:
            logger.debug("tool_scores table not found — starting fresh")
        finally:
            conn.close()

    @classmethod
    def from_db(cls, db_path: str) -> ToolScoreIndex:
        """Create an index pre-loaded from a SQLite database."""
        index = cls()
        index.load(db_path)
        return index
