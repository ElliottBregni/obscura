from __future__ import annotations

import math
from datetime import UTC, datetime

import pytest
from qdrant_client.models import PointStruct

from obscura.memory import MemoryKey
from obscura.vector_memory.backends import BackendConfig
from obscura.vector_memory.backends.qdrant_backend import QdrantBackend, _point_id


def _unit(vec: list[float]) -> list[float]:
    """Qdrant collections with cosine distance store vectors L2-normalized,
    so a round-tripped embedding only matches the input if the input is already
    unit-length."""
    norm = math.sqrt(sum(x * x for x in vec))
    return [x / norm for x in vec]


def _make_backend(dim: int = 4) -> QdrantBackend:
    config = BackendConfig(user_id="u-qdrant-with-vectors-test", embedding_dim=dim)
    return QdrantBackend(config=config, mode="memory")


def _insert(
    backend: QdrantBackend,
    namespace: str,
    key: str,
    embedding: list[float],
    memory_type: str = "general",
) -> None:
    backend.client.upsert(
        backend.collection_name,
        [
            PointStruct(
                id=_point_id(namespace, key),
                vector=embedding,
                payload={
                    "namespace": namespace,
                    "key": key,
                    "text": f"text for {key}",
                    "metadata": {},
                    "memory_type": memory_type,
                    "created_at": datetime.now(UTC).isoformat(),
                },
            ),
        ],
    )


def test_search_vectors_default_returns_empty_embedding() -> None:
    """Default behavior preserves cost characteristics — no vector round-trip."""
    backend = _make_backend()
    embedding = _unit([0.1, 0.2, 0.3, 0.4])
    _insert(backend, "default", "alpha", embedding)

    results = backend.search_vectors(
        query_embedding=embedding,
        namespace="default",
        top_k=10,
    )
    assert len(results) == 1
    assert results[0].embedding == []


def test_search_vectors_with_vectors_returns_original_embedding() -> None:
    backend = _make_backend()
    embedding = _unit([0.5, -0.25, 0.75, -1.0])
    _insert(backend, "default", "beta", embedding)

    results = backend.search_vectors(
        query_embedding=embedding,
        namespace="default",
        top_k=10,
        with_vectors=True,
    )
    assert len(results) == 1
    assert results[0].key == MemoryKey(namespace="default", key="beta")
    assert results[0].embedding == pytest.approx(embedding, rel=1e-5)


def test_list_by_type_default_returns_empty_embedding() -> None:
    backend = _make_backend()
    _insert(
        backend,
        "default",
        "gamma",
        _unit([0.1, 0.1, 0.1, 0.1]),
        memory_type="episode",
    )

    results = backend.list_by_type("episode")
    assert len(results) == 1
    assert results[0].embedding == []


def test_list_by_type_with_vectors_returns_original_embedding() -> None:
    backend = _make_backend()
    embedding = _unit([-0.3, 0.6, -0.9, 0.0])
    _insert(backend, "default", "delta", embedding, memory_type="episode")

    results = backend.list_by_type("episode", with_vectors=True)
    assert len(results) == 1
    assert results[0].key == MemoryKey(namespace="default", key="delta")
    assert results[0].embedding == pytest.approx(embedding, rel=1e-5)
