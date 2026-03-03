"""Tests for sdk.vector_memory — Semantic memory with vector search."""

# pyright: reportUnknownMemberType=false
from __future__ import annotations

from pathlib import Path

import pytest

from obscura.auth.models import AuthenticatedUser
from obscura.vector_memory import VectorMemoryStore, cosine_similarity, simple_embedding
from obscura.vector_memory.backends import BackendConfig, SQLiteBackend


@pytest.fixture
def test_user() -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id="u-vector-test",
        email="vector@test.com",
        roles=("admin",),
        org_id="org-1",
        token_type="user",
        raw_token="test",
    )


@pytest.fixture
def temp_db(tmp_path: Path) -> Path:
    return tmp_path / "vector_memory.db"


@pytest.fixture
def store(test_user: AuthenticatedUser, temp_db: Path) -> VectorMemoryStore:
    """VectorMemoryStore backed by a temp SQLite DB (isolated per test)."""
    config = BackendConfig(user_id=test_user.user_id, embedding_dim=384)
    backend = SQLiteBackend(config=config, db_path=temp_db)
    return VectorMemoryStore(test_user, backend=backend)


class TestEmbeddingFunctions:
    def test_simple_embedding_deterministic(self) -> None:
        """Same text should produce same embedding."""
        emb1 = simple_embedding("hello world")
        emb2 = simple_embedding("hello world")
        assert emb1 == emb2

    def test_simple_embedding_different_text(self) -> None:
        """Different text should produce different embeddings."""
        emb1 = simple_embedding("hello world")
        emb2 = simple_embedding("goodbye world")
        assert emb1 != emb2

    def test_simple_embedding_dimensions(self) -> None:
        """Embedding should have correct dimensions."""
        emb = simple_embedding("test", dim=128)
        assert len(emb) == 128

    def test_cosine_similarity_same_vector(self) -> None:
        """Cosine similarity of identical vectors is 1.0."""
        vec = [1.0, 2.0, 3.0]
        sim = cosine_similarity(vec, vec)
        assert sim == pytest.approx(1.0, abs=0.001)

    def test_cosine_similarity_orthogonal(self) -> None:
        """Cosine similarity of orthogonal vectors is 0.0."""
        vec1 = [1.0, 0.0, 0.0]
        vec2 = [0.0, 1.0, 0.0]
        sim = cosine_similarity(vec1, vec2)
        assert sim == pytest.approx(0.0, abs=0.001)

    def test_cosine_similarity_opposite(self) -> None:
        """Cosine similarity of opposite vectors is -1.0."""
        vec1 = [1.0, 2.0, 3.0]
        vec2 = [-1.0, -2.0, -3.0]
        sim = cosine_similarity(vec1, vec2)
        assert sim == pytest.approx(-1.0, abs=0.001)


class TestVectorMemoryStore:
    def test_set_and_get(self, store: VectorMemoryStore) -> None:
        store.set("key1", "This is a test memory", metadata={"tag": "test"})

        entry = store.get("key1")
        assert entry is not None
        assert entry.text == "This is a test memory"
        assert entry.metadata == {"tag": "test"}
        assert len(entry.embedding) > 0

    def test_get_missing(self, store: VectorMemoryStore) -> None:
        entry = store.get("nonexistent")
        assert entry is None

    def test_update_existing(self, store: VectorMemoryStore) -> None:
        store.set("key1", "original text")
        store.set("key1", "updated text", metadata={"updated": True})

        entry = store.get("key1")
        assert entry is not None
        assert entry.text == "updated text"
        assert entry.metadata == {"updated": True}

    def test_delete(self, store: VectorMemoryStore) -> None:
        store.set("key1", "text to delete")

        deleted = store.delete("key1")
        assert deleted is True

        entry = store.get("key1")
        assert entry is None

    def test_delete_missing(self, store: VectorMemoryStore) -> None:
        deleted = store.delete("nonexistent")
        assert deleted is False

    def test_search_similar_basic(self, store: VectorMemoryStore) -> None:
        # Store some memories
        store.set("python", "Python is a programming language")
        store.set("javascript", "JavaScript runs in browsers")
        store.set("cooking", "Cooking is an art of preparing food")

        # Search for programming-related query
        results = store.search_similar("coding language", top_k=2)

        # Should return programming-related results
        assert len(results) == 2
        # Python should rank higher than cooking for "coding language"
        texts = [r.text for r in results]
        assert "Python" in texts[0] or "JavaScript" in texts[0]

    def test_search_similar_with_namespace(self, store: VectorMemoryStore) -> None:
        store.set("doc1", "Python tutorial", namespace="tutorials")
        store.set("doc2", "JavaScript guide", namespace="tutorials")
        store.set("note1", "Grocery list", namespace="notes")

        results = store.search_similar("programming", namespace="tutorials", top_k=5)

        assert len(results) == 2
        for r in results:
            assert r.key.namespace == "tutorials"

    def test_search_similar_threshold(self, store: VectorMemoryStore) -> None:
        store.set("a", "Python is great")
        store.set("b", "Completely unrelated topic about flowers")

        # High threshold should filter out low similarity
        results = store.search_similar("python programming", threshold=0.99)
        # With simple hash embeddings, might not get high similarity
        # This test mainly checks the threshold parameter works
        for r in results:
            assert r.score >= 0.99

    def test_list_keys(self, store: VectorMemoryStore) -> None:
        store.set("key1", "text1", namespace="ns1")
        store.set("key2", "text2", namespace="ns1")
        store.set("key3", "text3", namespace="ns2")

        all_keys = store.list_keys()
        assert len(all_keys) == 3

        ns1_keys = store.list_keys(namespace="ns1")
        assert len(ns1_keys) == 2

    def test_clear_namespace(self, store: VectorMemoryStore) -> None:
        store.set("key1", "text1", namespace="ns1")
        store.set("key2", "text2", namespace="ns2")

        count = store.clear_namespace("ns1")
        assert count == 1

        assert store.get("key1", namespace="ns1") is None
        assert store.get("key2", namespace="ns2") is not None

    def test_get_stats(self, store: VectorMemoryStore) -> None:
        store.set("key1", "text1", namespace="ns1")
        store.set("key2", "text2", namespace="ns2")

        stats = store.get_stats()
        assert stats["total_memories"] == 2
        assert "embedding_dim" in stats
        assert stats["namespaces"]["ns1"] == 1
        assert stats["namespaces"]["ns2"] == 1

    def test_singleton_per_user(self, test_user: AuthenticatedUser) -> None:
        store1 = VectorMemoryStore.for_user(test_user)
        store2 = VectorMemoryStore.for_user(test_user)
        assert store1 is store2


class TestSemanticMemoryMixin:
    """Tests for the SemanticMemoryMixin (would need Agent class to test fully)."""

    def test_remember_creates_entry(self, store: VectorMemoryStore) -> None:
        # This would require an Agent instance
        # Just test that VectorMemoryStore can be used standalone

        # Simulate what remember() does
        store.set(
            "memory_123",
            "I learned about Python async",
            metadata={"agent_id": "agent-1", "agent_name": "learner"},
            namespace="default:semantic",
        )

        entry = store.get("memory_123", namespace="default:semantic")
        assert entry is not None
        assert entry.metadata["agent_name"] == "learner"

    def test_recall_finds_similar(self, store: VectorMemoryStore) -> None:
        # Simulate memories
        store.set(
            "mem1",
            "Python async/await is for concurrency",
            namespace="default:semantic",
        )
        store.set(
            "mem2", "JavaScript promises are asynchronous", namespace="default:semantic"
        )
        store.set(
            "mem3", "Cooking pasta requires boiling water", namespace="default:semantic"
        )

        # Simulate recall()
        results = store.search_similar(
            "how do I run async code?", namespace="default:semantic", top_k=2
        )

        assert len(results) == 2
        # Programming memories should rank higher
        texts = " ".join([r.text for r in results])
        assert "Python" in texts or "JavaScript" in texts
