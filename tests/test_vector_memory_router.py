"""Tests for sdk.vector_memory_router."""
import pytest
from unittest.mock import MagicMock
from datetime import UTC, datetime
from sdk.vector_memory_router import MemoryRouter, MemoryTypeQuery, RoutedResult
from sdk.vector_memory import VectorMemoryEntry
from sdk.memory import MemoryKey


def _make_entry(ns="test", key="k1", text="hello", score=0.9):
    e = VectorMemoryEntry(
        key=MemoryKey(namespace=ns, key=key),
        text=text,
        embedding=[0.1],
        metadata={},
        created_at=datetime.now(UTC),
        final_score=score,
    )
    return e


class TestMemoryTypeQuery:
    def test_defaults(self):
        q = MemoryTypeQuery(memory_type="fact")
        assert q.weight == 1.0
        assert q.top_k == 10
        assert q.reranker is None


class TestRoutedResult:
    def test_construction(self):
        r = RoutedResult(entries=[], sources={"fact": 0})
        assert r.entries == []
        assert r.sources == {"fact": 0}


class TestMemoryRouter:
    def test_single_route(self):
        store = MagicMock()
        e1 = _make_entry(key="k1", score=0.9)
        store.search_reranked.return_value = [e1]

        router = MemoryRouter(store)
        result = router.route_and_merge(
            query="test",
            routes=[MemoryTypeQuery("fact", weight=1.0, top_k=5)],
        )
        assert len(result.entries) == 1
        assert result.sources == {"fact": 1}

    def test_multiple_routes(self):
        store = MagicMock()
        e1 = _make_entry(ns="a", key="k1", score=0.9)
        e2 = _make_entry(ns="b", key="k2", score=0.8)
        store.search_reranked.side_effect = [[e1], [e2]]

        router = MemoryRouter(store)
        result = router.route_and_merge(
            query="test",
            routes=[
                MemoryTypeQuery("fact", weight=0.6, top_k=5),
                MemoryTypeQuery("episode", weight=0.4, top_k=5),
            ],
        )
        assert len(result.entries) == 2
        assert result.sources == {"fact": 1, "episode": 1}

    def test_weight_applied(self):
        store = MagicMock()
        e1 = _make_entry(key="k1", score=1.0)
        store.search_reranked.return_value = [e1]

        router = MemoryRouter(store)
        result = router.route_and_merge(
            query="test",
            routes=[MemoryTypeQuery("fact", weight=0.5, top_k=5)],
        )
        assert abs(result.entries[0].final_score - 0.5) < 1e-9

    def test_final_top_k(self):
        store = MagicMock()
        entries = [_make_entry(key=f"k{i}", score=1.0 - i * 0.1) for i in range(5)]
        store.search_reranked.return_value = entries

        router = MemoryRouter(store)
        result = router.route_and_merge(
            query="test",
            routes=[MemoryTypeQuery("fact", weight=1.0, top_k=10)],
            final_top_k=3,
        )
        assert len(result.entries) == 3

    def test_dedup_keeps_highest(self):
        # Same (namespace, key) with different scores
        e1 = _make_entry(ns="a", key="k1", score=0.5)
        e2 = _make_entry(ns="a", key="k1", score=0.9)
        merged = MemoryRouter._dedupe_and_sort([e1, e2])
        assert len(merged) == 1
        assert merged[0].final_score == 0.9

    def test_dedup_different_keys(self):
        e1 = _make_entry(ns="a", key="k1", score=0.9)
        e2 = _make_entry(ns="a", key="k2", score=0.8)
        merged = MemoryRouter._dedupe_and_sort([e1, e2])
        assert len(merged) == 2

    def test_empty_routes(self):
        store = MagicMock()
        router = MemoryRouter(store)
        result = router.route_and_merge(query="test", routes=[])
        assert result.entries == []
        assert result.sources == {}

    def test_search_reranked_params(self):
        store = MagicMock()
        store.search_reranked.return_value = []

        router = MemoryRouter(store)
        router.route_and_merge(
            query="hello",
            routes=[MemoryTypeQuery("fact", weight=1.0, top_k=5)],
            namespace="ns1",
            threshold=0.5,
            first_stage_k=25,
        )
        store.search_reranked.assert_called_once_with(
            query="hello",
            namespace="ns1",
            top_k=5,
            first_stage_k=25,
            threshold=0.5,
            memory_types=["fact"],
            metadata_filters=None,
            reranker=None,
        )
