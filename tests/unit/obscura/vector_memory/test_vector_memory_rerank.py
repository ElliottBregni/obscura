"""Tests for sdk.vector_memory_rerank."""

from datetime import UTC, datetime, timedelta

import pytest

from obscura.memory import MemoryKey
from obscura.vector_memory import VectorMemoryEntry
from obscura.vector_memory.scoring import HybridWeights
from obscura.vector_memory.vector_memory_rerank import (
    BM25Reranker,
    CompositeReranker,
    MetadataReranker,
    RecencyReranker,
)


def _make_entry(
    text: str = "hello world",
    created_at: datetime | None = None,
    metadata: dict[str, object] | None = None,
    score: float = 0.0,
) -> VectorMemoryEntry:
    return VectorMemoryEntry(
        key=MemoryKey(namespace="test", key="k1"),
        text=text,
        embedding=[0.1, 0.2],
        metadata=metadata or {},
        memory_type="general",
        created_at=created_at or datetime.now(UTC),
        score=score,
    )


class TestBM25Reranker:
    def test_exact_match(self) -> None:
        r = BM25Reranker()
        entry = _make_entry(text="hello world foo bar")
        score = r.score("hello world", entry, [])
        assert score > 0

    def test_no_match(self) -> None:
        r = BM25Reranker()
        entry = _make_entry(text="completely different text")
        score = r.score("hello world", entry, [])
        assert score == 0.0

    def test_empty_query(self) -> None:
        r = BM25Reranker()
        entry = _make_entry(text="hello world")
        assert r.score("", entry, []) == 0.0

    def test_empty_doc(self) -> None:
        r = BM25Reranker()
        entry = _make_entry(text="")
        assert r.score("hello", entry, []) == 0.0

    def test_partial_match(self) -> None:
        r = BM25Reranker()
        entry = _make_entry(text="hello world foo bar")
        full = r.score("hello world", entry, [])
        partial = r.score("hello xyz", entry, [])
        assert full > partial > 0


class TestRecencyReranker:
    """RecencyReranker now blends vector_sim + decay + usage via hybrid_score.

    Defaults: vector=0.7, decay=0.25, usage=0.05, graph=0.0.
    With vector_sim=0 (default in the test fixture), recent entries score
    approximately the decay weight (0.25); old entries score near 0.
    """

    def test_recent_entry_higher_than_old(self) -> None:
        r = RecencyReranker(decay_days=30)
        recent = _make_entry(created_at=datetime.now(UTC))
        old = _make_entry(created_at=datetime.now(UTC) - timedelta(days=365))
        assert r.score("q", recent, []) > r.score("q", old, [])

    def test_recent_with_vector_sim_dominates(self) -> None:
        r = RecencyReranker(decay_days=30)
        entry = _make_entry(created_at=datetime.now(UTC), score=1.0)
        score = r.score("q", entry, [])
        # vector(0.7*1) + decay(0.25*~1) ≈ 0.95
        assert score > 0.9

    def test_old_entry_low_score(self) -> None:
        r = RecencyReranker(decay_days=30)
        entry = _make_entry(created_at=datetime.now(UTC) - timedelta(days=365))
        score = r.score("q", entry, [])
        assert score < 0.05

    def test_weight_outer_scales(self) -> None:
        w = HybridWeights()
        r1 = RecencyReranker(decay_days=30, weight=1.0, weights=w)
        r2 = RecencyReranker(decay_days=30, weight=2.0, weights=w)
        entry = _make_entry(created_at=datetime.now(UTC), score=1.0)
        s1 = r1.score("q", entry, [])
        s2 = r2.score("q", entry, [])
        assert s2 == pytest.approx(2 * s1, rel=1e-6)

    def test_naive_datetime_normalised(self) -> None:
        r = RecencyReranker(decay_days=30)
        entry = _make_entry(created_at=datetime.now())  # naive
        # Should not raise even with naive datetime.
        r.score("q", entry, [])

    def test_usage_count_boosts_score(self) -> None:
        r = RecencyReranker(decay_days=30)
        entry_no_use = _make_entry(
            created_at=datetime.now(UTC),
            metadata={"access_count": 0},
        )
        entry_high_use = _make_entry(
            created_at=datetime.now(UTC),
            metadata={"access_count": 100},
        )
        assert r.score("q", entry_high_use, []) > r.score("q", entry_no_use, [])

    def test_missing_access_count_treated_as_zero(self) -> None:
        r = RecencyReranker(decay_days=30)
        # No access_count metadata at all.
        entry = _make_entry(created_at=datetime.now(UTC))
        # Should not raise.
        r.score("q", entry, [])


class TestMetadataReranker:
    def test_boost_present(self) -> None:
        r = MetadataReranker(boost_keys={"important": 0.5})
        entry = _make_entry(metadata={"important": True})
        assert r.score("q", entry, []) == 0.5

    def test_boost_absent(self) -> None:
        r = MetadataReranker(boost_keys={"important": 0.5})
        entry = _make_entry(metadata={})
        assert r.score("q", entry, []) == 0.0

    def test_boost_falsy(self) -> None:
        r = MetadataReranker(boost_keys={"important": 0.5})
        entry = _make_entry(metadata={"important": False})
        assert r.score("q", entry, []) == 0.0

    def test_multiple_keys(self) -> None:
        r = MetadataReranker(boost_keys={"a": 0.3, "b": 0.2})
        entry = _make_entry(metadata={"a": True, "b": True})
        assert abs(r.score("q", entry, []) - 0.5) < 1e-9


class TestCompositeReranker:
    def test_weighted_combination(self) -> None:
        r = CompositeReranker(
            rerankers=[
                (BM25Reranker(), 0.5),
                (RecencyReranker(decay_days=30), 0.5),
            ],
        )
        entry = _make_entry(text="hello world", created_at=datetime.now(UTC))
        score = r.score("hello world", entry, [])
        assert score > 0

    def test_single_reranker(self) -> None:
        r = CompositeReranker(rerankers=[(RecencyReranker(decay_days=30), 1.0)])
        entry = _make_entry(created_at=datetime.now(UTC), score=1.0)
        score = r.score("q", entry, [])
        # vector(0.7) + decay(0.25) = 0.95 with default hybrid weights.
        assert score > 0.9

    def test_empty_rerankers(self) -> None:
        r = CompositeReranker(rerankers=[])
        entry = _make_entry()
        assert r.score("q", entry, []) == 0.0
