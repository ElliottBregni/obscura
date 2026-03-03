"""Tests for memory commit gating and deduplication."""

from __future__ import annotations

from pathlib import Path

import pytest

from obscura.core.supervisor.memory_gate import (
    MemoryCommitGate,
    compute_memory_score,
    content_hash,
    recency_decay,
)
from obscura.core.supervisor.types import MemoryCandidate


@pytest.fixture
def gate(tmp_path: Path) -> MemoryCommitGate:
    g = MemoryCommitGate(
        db_path=tmp_path / "test.db",
        session_id="sess-1",
        run_id="run-1",
        min_importance=0.3,
        max_batch_size=5,
    )
    yield g
    g.close()


class TestMemoryCommitGate:
    def test_basic_commit(self, gate: MemoryCommitGate) -> None:
        gate.queue_item("fact-1", "The sky is blue", importance=0.8)
        result = gate.commit_sync()
        assert result.committed == 1
        assert result.deduplicated == 0
        assert result.gated == 0

    def test_deduplication_within_batch(self, gate: MemoryCommitGate) -> None:
        h = content_hash("same content")
        gate.queue(MemoryCandidate(key="a", content="same content", content_hash=h, importance=0.8))
        gate.queue(MemoryCandidate(key="b", content="same content", content_hash=h, importance=0.8))
        result = gate.commit_sync()
        assert result.committed == 1
        assert result.deduplicated == 1

    def test_deduplication_across_runs(self, tmp_path: Path) -> None:
        # First run
        g1 = MemoryCommitGate(
            db_path=tmp_path / "test.db",
            session_id="sess-1",
            run_id="run-1",
        )
        g1.queue_item("fact-1", "The sky is blue", importance=0.8)
        g1.commit_sync()

        # Second run — same content
        g2 = MemoryCommitGate(
            db_path=tmp_path / "test.db",
            session_id="sess-1",
            run_id="run-2",
        )
        g2.queue_item("fact-1", "The sky is blue", importance=0.8)
        result = g2.commit_sync()
        assert result.committed == 0
        assert result.deduplicated == 1
        g1.close()
        g2.close()

    def test_importance_gating(self, gate: MemoryCommitGate) -> None:
        gate.queue_item("low", "low importance", importance=0.1)
        gate.queue_item("high", "high importance", importance=0.9)
        result = gate.commit_sync()
        assert result.committed == 1
        assert result.gated == 1

    def test_pinned_bypasses_gating(self, gate: MemoryCommitGate) -> None:
        gate.queue_item("pinned", "pinned content", importance=0.1, pinned=True)
        result = gate.commit_sync()
        assert result.committed == 1
        assert result.gated == 0

    def test_batch_size_limit(self, gate: MemoryCommitGate) -> None:
        for i in range(10):
            gate.queue_item(f"fact-{i}", f"content {i}", importance=0.8)
        result = gate.commit_sync()
        assert result.committed == 5  # max_batch_size
        assert result.gated == 5

    def test_empty_queue(self, gate: MemoryCommitGate) -> None:
        result = gate.commit_sync()
        assert result.committed == 0

    def test_events_emitted(self, gate: MemoryCommitGate) -> None:
        gate.queue_item("fact-1", "content", importance=0.8)
        gate.commit_sync()
        assert len(gate.events) > 0


class TestScoringHelpers:
    def test_recency_decay_fresh(self) -> None:
        assert recency_decay(0) == 1.0

    def test_recency_decay_old(self) -> None:
        score = recency_decay(100)
        assert 0 < score < 0.1

    def test_compute_memory_score(self) -> None:
        score = compute_memory_score(
            importance=1.0,
            relevance=1.0,
            age_hours=0,
        )
        assert 0.9 < score <= 1.0

    def test_content_hash_deterministic(self) -> None:
        h1 = content_hash("hello world")
        h2 = content_hash("hello world")
        assert h1 == h2

    def test_content_hash_different(self) -> None:
        h1 = content_hash("hello")
        h2 = content_hash("world")
        assert h1 != h2
