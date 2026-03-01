"""Tests for sdk.vector_memory."""

from unittest.mock import MagicMock
from datetime import UTC, datetime

from obscura.vector_memory import MemoryRouter, MemoryTypeQuery, RoutedResult
from obscura.vector_memory import VectorMemoryEntry
from obscura.memory import MemoryKey


def _make_entry(
    ns: str = "test",
    key: str = "k1",
    text: str = "hello",
    score: float = 0.9,
) -> VectorMemoryEntry:
    """Helper to build a typed VectorMemoryEntry."""
    return VectorMemoryEntry(
        key=MemoryKey(namespace=ns, key=key),
        text=text,
        embedding=[0.1],
        metadata={},
        created_at=datetime.now(UTC),
        score=score,
        memory_type="general",
    )


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
    def test_single_route(self) -> None:
        store: MagicMock = MagicMock()
        e1 = _make_entry(key="k1", score=0.9)
        store.search_reranked.return_value = [e1]

        router = MemoryRouter(store)
        result = router.route_and_merge(
            query="test",
            routes=[MemoryTypeQuery("fact", weight=1.0, top_k=5)],
        )
        assert len(result.entries) == 1
        assert result.sources == {"fact": 1}

    def test_multiple_routes(self) -> None:
        store: MagicMock = MagicMock()
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

    def test_weight_applied(self) -> None:
        store: MagicMock = MagicMock()
        e1 = _make_entry(key="k1", score=1.0)
        store.search_reranked.return_value = [e1]

        router = MemoryRouter(store)
        result = router.route_and_merge(
            query="test",
            routes=[MemoryTypeQuery("fact", weight=0.5, top_k=5)],
        )
        assert abs(result.entries[0].final_score - 0.5) < 1e-9

    def test_final_top_k(self) -> None:
        store: MagicMock = MagicMock()
        entries = [_make_entry(key=f"k{i}", score=1.0 - i * 0.1) for i in range(5)]
        store.search_reranked.return_value = entries

        router = MemoryRouter(store)
        result = router.route_and_merge(
            query="test",
            routes=[MemoryTypeQuery("fact", weight=1.0, top_k=10)],
            final_top_k=3,
        )
        assert len(result.entries) == 3

    def test_dedup_keeps_highest(self) -> None:
        # Same (namespace, key) with different scores
        e1 = _make_entry(ns="a", key="k1", score=0.5)
        e2 = _make_entry(ns="a", key="k1", score=0.9)
        merged = MemoryRouter._dedupe_and_sort([e1, e2])  # type: ignore[reportPrivateUsage]
        assert len(merged) == 1
        assert merged[0].final_score == 0.9

    def test_dedup_different_keys(self) -> None:
        e1 = _make_entry(ns="a", key="k1", score=0.9)
        e2 = _make_entry(ns="a", key="k2", score=0.8)
        merged = MemoryRouter._dedupe_and_sort([e1, e2])  # type: ignore[reportPrivateUsage]
        assert len(merged) == 2

    def test_empty_routes(self) -> None:
        store: MagicMock = MagicMock()
        router = MemoryRouter(store)
        result = router.route_and_merge(query="test", routes=[])
        assert result.entries == []
        assert result.sources == {}

    def test_search_reranked_params(self) -> None:
        store: MagicMock = MagicMock()
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
