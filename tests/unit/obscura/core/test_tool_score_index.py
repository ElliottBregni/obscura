"""Tests for obscura.core.tool_score_index."""

from __future__ import annotations

import time
from typing import Any

from obscura.core.tool_score_index import ToolScore, ToolScoreIndex
from obscura.plugins.broker import BrokerAuditEntry


def _make_entry(
    tool: str = "test_tool",
    action: str = "executed",
    latency_ms: int = 100,
    error: str = "",
) -> BrokerAuditEntry:
    return BrokerAuditEntry(
        call_id="c1",
        tool=tool,
        agent_id="agent-1",
        action=action,
        latency_ms=latency_ms,
        error=error,
        timestamp=time.time(),
    )


class TestToolScore:
    def test_neutral_defaults(self) -> None:
        s = ToolScore(name="unknown")
        assert s.success_rate == 0.5  # neutral for no invocations
        assert s.error_rate == 0.0
        assert s.avg_latency_ms == 0.0
        assert s.quality_score == 0.5

    def test_perfect_success(self) -> None:
        s = ToolScore(
            name="t",
            invocation_count=10,
            success_count=10,
            last_used=time.time(),
        )
        assert s.success_rate == 1.0
        assert s.error_rate == 0.0
        assert s.quality_score > 0.5

    def test_all_errors(self) -> None:
        s = ToolScore(
            name="t",
            invocation_count=10,
            success_count=0,
            error_count=10,
            last_used=time.time(),
        )
        assert s.success_rate == 0.0
        assert s.error_rate == 1.0
        assert s.quality_score < 0.5


class TestToolScoreIndex:
    def test_record_and_get(self) -> None:
        index = ToolScoreIndex()
        index.record(_make_entry(tool="shell", action="executed", latency_ms=50))
        score = index.get_score("shell")
        assert score.invocation_count == 1
        assert score.success_count == 1
        assert score.total_latency_ms == 50

    def test_record_error(self) -> None:
        index = ToolScoreIndex()
        index.record(_make_entry(tool="bad", action="error", error="boom"))
        score = index.get_score("bad")
        assert score.error_count == 1
        assert score.last_error == "boom"

    def test_unknown_tool_returns_neutral(self) -> None:
        index = ToolScoreIndex()
        score = index.get_score("nonexistent")
        assert score.quality_score == 0.5
        assert score.invocation_count == 0

    def test_ranked_order(self) -> None:
        index = ToolScoreIndex()
        # Give 'good' 10 successes
        for _ in range(10):
            index.record(_make_entry(tool="good", action="executed", latency_ms=50))
        # Give 'bad' 10 errors
        for _ in range(10):
            index.record(_make_entry(tool="bad", action="error", latency_ms=5000))

        ranked = index.ranked(["bad", "good"])
        assert ranked[0] == "good"
        assert ranked[1] == "bad"

    def test_get_scores_bulk(self) -> None:
        index = ToolScoreIndex()
        index.record(_make_entry(tool="a"))
        scores = index.get_scores(["a", "b"])
        assert "a" in scores
        assert "b" in scores
        assert scores["a"].invocation_count == 1
        assert scores["b"].invocation_count == 0

    def test_known_tools(self) -> None:
        index = ToolScoreIndex()
        index.record(_make_entry(tool="x"))
        assert "x" in index.known_tools
        assert len(index) == 1


class TestSQLitePersistence:
    def test_save_and_load(self, tmp_path: Any) -> None:
        db = str(tmp_path / "scores.db")
        index = ToolScoreIndex()
        for _ in range(5):
            index.record(
                _make_entry(tool="persisted_tool", action="executed", latency_ms=100),
            )
        index.record(_make_entry(tool="persisted_tool", action="error", latency_ms=200))
        index.save(db)

        loaded = ToolScoreIndex.from_db(db)
        score = loaded.get_score("persisted_tool")
        assert score.invocation_count == 6
        assert score.success_count == 5
        assert score.error_count == 1

    def test_load_missing_db(self, tmp_path: Any) -> None:
        db = str(tmp_path / "nonexistent.db")
        index = ToolScoreIndex.from_db(db)
        assert len(index) == 0

    def test_save_empty_index(self, tmp_path: Any) -> None:
        db = str(tmp_path / "empty.db")
        index = ToolScoreIndex()
        index.save(db)
        loaded = ToolScoreIndex.from_db(db)
        assert len(loaded) == 0
