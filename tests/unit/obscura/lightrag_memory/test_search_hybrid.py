"""Tests for HybridVectorMemoryStore.search_hybrid()."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

from obscura.lightrag_memory.adapter import GraphHit
from obscura.memory import MemoryKey

from .assert_helpers import assert_score_decreasing


def _seed_with_hit(
    store,
    mock_lr,
    *,
    key: str,
    text: str,
    namespace: str = "default",
    memory_type: str = "fact",
    vector_sim: float = 0.5,
    graph_relevance: float = 0.5,
    created_at: datetime | None = None,
    accessed_at: datetime | None = None,
    access_count: int = 0,
    query_substring: str = "default-query",
) -> None:
    """Persist a chunk via store.set() and register the corresponding hit."""
    metadata = {"access_count": access_count}
    store.set(
        key, text, metadata=metadata, namespace=namespace, memory_type=memory_type
    )
    if created_at or accessed_at:
        entry = store.backend.get_vector(MemoryKey(namespace=namespace, key=key))
        if entry and created_at:
            entry.created_at = created_at
        if entry and accessed_at:
            entry.accessed_at = accessed_at
        store.backend.store_vector(
            key=entry.key,
            text=entry.text,
            embedding=entry.embedding,
            metadata=entry.metadata,
            memory_type=entry.memory_type,
            expires_at=None,
        )

    hit = GraphHit(
        namespace=namespace,
        key=key,
        vector_sim=vector_sim,
        graph_relevance=graph_relevance,
        text_excerpt=text[:80],
    )
    found = False
    for sub, hits in mock_lr.state.canned_aquery:
        if sub == query_substring:
            hits.append(hit)
            found = True
            break
    if not found:
        mock_lr.set_canned(query_substring, [hit])


class TestSearchHybridBasic:
    def test_returns_in_score_descending_order(
        self, hybrid_store, mock_lightrag
    ) -> None:
        """5 seeded chunks → final ordering matches the manually-computed scores."""
        _seed_with_hit(
            hybrid_store,
            mock_lightrag,
            key="k1",
            text="content one " * 5,
            vector_sim=0.9,
            graph_relevance=0.9,
        )
        _seed_with_hit(
            hybrid_store,
            mock_lightrag,
            key="k2",
            text="content two " * 5,
            vector_sim=0.8,
            graph_relevance=0.7,
        )
        _seed_with_hit(
            hybrid_store,
            mock_lightrag,
            key="k3",
            text="content three " * 5,
            vector_sim=0.5,
            graph_relevance=0.6,
        )
        _seed_with_hit(
            hybrid_store,
            mock_lightrag,
            key="k4",
            text="content four " * 5,
            vector_sim=0.3,
            graph_relevance=0.4,
        )
        _seed_with_hit(
            hybrid_store,
            mock_lightrag,
            key="k5",
            text="content five " * 5,
            vector_sim=0.1,
            graph_relevance=0.2,
        )

        results = hybrid_store.search_hybrid("default-query", top_k=5)
        assert len(results) == 5
        assert_score_decreasing(results)
        assert results[0].key.key == "k1"

    def test_top_k_caps_results(self, hybrid_store, mock_lightrag) -> None:
        for i in range(10):
            _seed_with_hit(
                hybrid_store,
                mock_lightrag,
                key=f"k{i}",
                text=f"content {i} " * 5,
                vector_sim=0.9 - 0.05 * i,
                graph_relevance=0.5,
            )
        results = hybrid_store.search_hybrid("default-query", top_k=3)
        assert len(results) == 3


class TestSearchHybridDecay:
    def test_decay_downweights_old_chunks(self, hybrid_store, mock_lightrag) -> None:
        """A 60-day-old chunk vs. a 1-day-old chunk — decay shifts the order."""
        now = datetime.now(UTC)
        _seed_with_hit(
            hybrid_store,
            mock_lightrag,
            key="old",
            text="old content " * 5,
            vector_sim=0.5,
            graph_relevance=0.5,
            created_at=now - timedelta(days=60),
        )
        _seed_with_hit(
            hybrid_store,
            mock_lightrag,
            key="fresh",
            text="fresh content " * 5,
            vector_sim=0.5,
            graph_relevance=0.5,
            created_at=now - timedelta(days=1),
        )

        results = hybrid_store.search_hybrid("default-query", top_k=2)
        assert len(results) == 2
        assert results[0].key.key == "fresh", (
            f"decay didn't downweight old chunk: {[r.key.key for r in results]}"
        )


class TestSearchHybridUsage:
    def test_usage_shifts_ordering(self, hybrid_store, mock_lightrag) -> None:
        """Among ties on vector + graph + decay, higher access_count wins."""
        from obscura.lightrag_memory.scoring import HybridWeights

        weights = HybridWeights(vector=0.4, graph=0.4, decay=0.0, usage=0.2)
        _seed_with_hit(
            hybrid_store,
            mock_lightrag,
            key="hot",
            text="hot content " * 5,
            vector_sim=0.5,
            graph_relevance=0.5,
            access_count=50,
        )
        _seed_with_hit(
            hybrid_store,
            mock_lightrag,
            key="cold",
            text="cold content " * 5,
            vector_sim=0.5,
            graph_relevance=0.5,
            access_count=0,
        )

        results = hybrid_store.search_hybrid("default-query", top_k=2, weights=weights)
        assert results[0].key.key == "hot"


class TestSearchHybridNamespace:
    def test_namespace_filter_returns_only_matching_ns(
        self, hybrid_store, mock_lightrag
    ) -> None:
        _seed_with_hit(
            hybrid_store,
            mock_lightrag,
            key="k1",
            text="content A1 " * 5,
            namespace="A",
            vector_sim=0.9,
            graph_relevance=0.9,
        )
        _seed_with_hit(
            hybrid_store,
            mock_lightrag,
            key="k2",
            text="content A2 " * 5,
            namespace="A",
            vector_sim=0.8,
            graph_relevance=0.8,
        )
        _seed_with_hit(
            hybrid_store,
            mock_lightrag,
            key="k3",
            text="content B1 " * 5,
            namespace="B",
            vector_sim=0.95,
            graph_relevance=0.95,
        )

        results = hybrid_store.search_hybrid("default-query", namespace="A", top_k=5)
        assert all(r.key.namespace == "A" for r in results)
        assert {r.key.key for r in results} == {"k1", "k2"}


class TestSearchHybridStaleRef:
    def test_drops_hits_for_missing_keys(self, hybrid_store, mock_lightrag) -> None:
        """LightRAG references a key that doesn't exist in the backend → silently drop."""
        _seed_with_hit(
            hybrid_store,
            mock_lightrag,
            key="k1",
            text="real content " * 5,
            vector_sim=0.5,
            graph_relevance=0.5,
        )
        mock_lightrag.set_canned(
            "default-query",
            [
                GraphHit(
                    namespace="default",
                    key="k1",
                    vector_sim=0.5,
                    graph_relevance=0.5,
                    text_excerpt="",
                ),
                GraphHit(
                    namespace="default",
                    key="phantom",
                    vector_sim=0.99,
                    graph_relevance=0.99,
                    text_excerpt="",
                ),
            ],
        )
        results = hybrid_store.search_hybrid("default-query", top_k=5)
        assert len(results) == 1
        assert results[0].key.key == "k1"


class TestSearchHybridFallback:
    def test_empty_aquery_falls_back_to_search_reranked(
        self,
        hybrid_store,
        mock_lightrag,
        monkeypatch,
    ) -> None:
        """When LightRAG returns no hits, fall back to plain `search_reranked`."""
        hybrid_store.set("k1", "fallback content here." * 3, memory_type="fact")

        called = {"super_search": False}
        from obscura.vector_memory import VectorMemoryStore

        original = VectorMemoryStore.search_reranked

        def _spy(self, *args, **kwargs):
            called["super_search"] = True
            return original(self, *args, **kwargs)

        monkeypatch.setattr(VectorMemoryStore, "search_reranked", _spy)

        results = hybrid_store.search_hybrid("nothing matches", top_k=5)
        assert called["super_search"] is True
        for r in results:
            assert hasattr(r, "final_score")

    def test_aquery_raises_falls_back(
        self,
        hybrid_store,
        mock_lightrag,
        monkeypatch,
    ) -> None:
        """When `aquery` raises, fall through to `search_reranked` — no propagation."""
        hybrid_store.set("k1", "fallback content." * 3, memory_type="fact")
        mock_lightrag.state.next_aquery_raises = RuntimeError("LR exploded")

        results = hybrid_store.search_hybrid("any query", top_k=5)
        assert isinstance(results, list)

    def test_aquery_timeout_falls_back(
        self,
        hybrid_store,
        mock_lightrag,
        monkeypatch,
    ) -> None:
        """If `aquery` exceeds timeout, fall back. Set a very low timeout via env."""
        monkeypatch.setenv("OBSCURA_LIGHTRAG_TIMEOUT_MS", "50")
        hybrid_store.set("k1", "fallback after timeout." * 3, memory_type="fact")
        mock_lightrag.state.next_aquery_sleep_s = 1.0

        t0 = time.monotonic()
        results = hybrid_store.search_hybrid("any query", top_k=5)
        elapsed = time.monotonic() - t0
        assert elapsed < 0.5, f"timeout not respected: {elapsed:.2f}s"
        assert isinstance(results, list)


class TestSearchHybridUsageIncrement:
    def test_search_increments_access_count(self, hybrid_store, mock_lightrag) -> None:
        """A query that returns a chunk bumps `metadata.access_count` by 1."""
        _seed_with_hit(
            hybrid_store,
            mock_lightrag,
            key="k1",
            text="incrementable content " * 5,
            vector_sim=0.9,
            graph_relevance=0.9,
            access_count=5,
        )

        results = hybrid_store.search_hybrid("default-query", top_k=1)
        assert results[0].key.key == "k1"

        for _ in range(20):
            entry = hybrid_store.get("k1")
            if entry and entry.metadata.get("access_count", 0) >= 6:
                break
            time.sleep(0.05)
        entry = hybrid_store.get("k1")
        assert entry is not None
        assert entry.metadata.get("access_count", 0) >= 6
