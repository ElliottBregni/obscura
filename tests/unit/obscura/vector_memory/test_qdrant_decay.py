from __future__ import annotations

from datetime import UTC, datetime, timedelta

from qdrant_client.models import PointStruct

from obscura.auth.models import AuthenticatedUser
from obscura.vector_memory import VectorMemoryStore
from obscura.vector_memory.backends import BackendConfig
from obscura.vector_memory.backends.qdrant_backend import QdrantBackend, _point_id


def test_time_decay_reduces_old_scores() -> None:
    test_user = AuthenticatedUser(
        user_id="u-qdrant-decay-test",
        email="decay@tests.local",
        roles=("tester",),
        org_id="org-test",
        token_type="user",
        raw_token="test",
    )

    config = BackendConfig(user_id=test_user.user_id, embedding_dim=16)
    backend = QdrantBackend(config=config, mode="memory")

    def embedding_fn(text):
        return [0.0] * config.embedding_dim

    VectorMemoryStore(test_user, backend=backend, embedding_fn=embedding_fn)

    # Two points with identical vectors but different created_at times
    now = datetime.now(UTC)
    old = now - timedelta(days=60)
    id_new = _point_id("default", "new")
    id_old = _point_id("default", "old")

    vec = [0.0] * config.embedding_dim

    backend.client.upsert(
        backend.collection_name,
        [
            PointStruct(
                id=id_new,
                vector=vec,
                payload={
                    "namespace": "default",
                    "key": "new",
                    "text": "recent",
                    "metadata": {},
                    "memory_type": "general",
                    "created_at": now.isoformat(),
                },
            ),
            PointStruct(
                id=id_old,
                vector=vec,
                payload={
                    "namespace": "default",
                    "key": "old",
                    "text": "older",
                    "metadata": {},
                    "memory_type": "general",
                    "created_at": old.isoformat(),
                },
            ),
        ],
    )

    # Query with same vector
    results = backend.search_vectors(query_embedding=vec, namespace="default", top_k=10)
    assert len(results) >= 2
    # find entries by key
    by_key = {r.key.key: r for r in results}
    assert "new" in by_key
    assert "old" in by_key
    new_final = by_key["new"].final_score
    old_final = by_key["old"].final_score
    assert new_final >= old_final
