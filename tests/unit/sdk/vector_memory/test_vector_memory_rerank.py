# pyright: ignore-all
"""Tests for sdk.vector_memory_rerank."""
from datetime import UTC, datetime, timedelta
from sdk.vector_memory_rerank import (
    BM25Reranker, RecencyReranker, MetadataReranker, CompositeReranker,
)
from sdk.memory import MemoryKey
from sdk.vector_memory import VectorMemoryEntry


def _make_entry(
    text: str = "hello world",
    created_at: datetime | None = None,
    metadata: dict[str, object] | None = None,
) -> VectorMemoryEntry:
    return VectorMemoryEntry(
        key=MemoryKey(namespace="test", key="k1"),
        text=text,
        embedding=[0.1, 0.2],
        metadata=metadata or {},
        created_at=created_at or datetime.now(UTC),
    )


class TestBM25Reranker:
    def test_exact_match(self):
        r = BM25Reranker()
        entry = _make_entry(text="hello world foo bar")
        score = r.score("hello world", entry, [])
        assert score > 0

    def test_no_match(self):
        r = BM25Reranker()
        entry = _make_entry(text="completely different text")
        score = r.score("hello world", entry, [])
        assert score == 0.0

    def test_empty_query(self):
        r = BM25Reranker()
        entry = _make_entry(text="hello world")
        assert r.score("", entry, []) == 0.0

    def test_empty_doc(self):
        r = BM25Reranker()
        entry = _make_entry(text="")
        assert r.score("hello", entry, []) == 0.0

    def test_partial_match(self):
        r = BM25Reranker()
        entry = _make_entry(text="hello world foo bar")
        full = r.score("hello world", entry, [])
        partial = r.score("hello xyz", entry, [])
        assert full > partial > 0


class TestRecencyReranker:
    def test_recent_entry(self):
        r = RecencyReranker(decay_days=30)
        entry = _make_entry(created_at=datetime.now(UTC))
        score = r.score("q", entry, [])
        assert score > 0.99  # very recent

    def test_old_entry(self):
        r = RecencyReranker(decay_days=30)
        entry = _make_entry(created_at=datetime.now(UTC) - timedelta(days=90))
        score = r.score("q", entry, [])
        assert score < 0.1  # exp(-90/30) ≈ 0.05

    def test_weight(self):
        r = RecencyReranker(decay_days=30, weight=2.0)
        entry = _make_entry(created_at=datetime.now(UTC))
        score = r.score("q", entry, [])
        assert score > 1.9

    def test_naive_datetime(self):
        r = RecencyReranker(decay_days=30)
        entry = _make_entry(created_at=datetime.now())  # naive
        score = r.score("q", entry, [])
        assert score > 0.99


class TestMetadataReranker:
    def test_boost_present(self):
        r = MetadataReranker(boost_keys={"important": 0.5})
        entry = _make_entry(metadata={"important": True})
        assert r.score("q", entry, []) == 0.5

    def test_boost_absent(self):
        r = MetadataReranker(boost_keys={"important": 0.5})
        entry = _make_entry(metadata={})
        assert r.score("q", entry, []) == 0.0

    def test_boost_falsy(self):
        r = MetadataReranker(boost_keys={"important": 0.5})
        entry = _make_entry(metadata={"important": False})
        assert r.score("q", entry, []) == 0.0

    def test_multiple_keys(self):
        r = MetadataReranker(boost_keys={"a": 0.3, "b": 0.2})
        entry = _make_entry(metadata={"a": True, "b": True})
        assert abs(r.score("q", entry, []) - 0.5) < 1e-9


class TestCompositeReranker:
    def test_weighted_combination(self):
        r = CompositeReranker(rerankers=[
            (BM25Reranker(), 0.5),
            (RecencyReranker(decay_days=30), 0.5),
        ])
        entry = _make_entry(text="hello world", created_at=datetime.now(UTC))
        score = r.score("hello world", entry, [])
        assert score > 0

    def test_single_reranker(self):
        r = CompositeReranker(rerankers=[(RecencyReranker(decay_days=30), 1.0)])
        entry = _make_entry(created_at=datetime.now(UTC))
        score = r.score("q", entry, [])
        assert score > 0.99

    def test_empty_rerankers(self):
        r = CompositeReranker(rerankers=[])
        entry = _make_entry()
        assert r.score("q", entry, []) == 0.0
