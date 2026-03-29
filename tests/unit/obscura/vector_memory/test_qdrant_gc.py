from __future__ import annotations

from datetime import UTC, datetime, timedelta

from obscura.auth.models import AuthenticatedUser
from obscura.vector_memory import VectorMemoryStore
from obscura.vector_memory.backends import BackendConfig
from obscura.vector_memory.backends.qdrant_backend import QdrantBackend, _point_id


def test_purge_expired_deletes_old_points():
    test_user = AuthenticatedUser(
        user_id="u-qdrant-gc-test",
        email="gc@tests.local",
        roles=("tester",),
        org_id="org-test",
        token_type="user",
        raw_token="test",
    )

    config = BackendConfig(user_id=test_user.user_id, embedding_dim=16)
    backend = QdrantBackend(config=config, mode="memory")
    # Ensure embedding function matches backend.embedding_dim to avoid local-client errors
    embedding_fn = lambda text: [0.0] * config.embedding_dim
    store = VectorMemoryStore(test_user, backend=backend, embedding_fn=embedding_fn)

    # Store an expired memory
    expires = datetime.now(UTC) - timedelta(seconds=10)
    store.set("old", "expired", ttl=None, metadata={}, namespace="default", memory_type="general")
    # Manually upsert with expires_at in payload to simulate expired entry
    point_id = _point_id("default", "old")
    # Directly set payload with expires_at in the past
    backend.client.upsert(
        backend.collection_name,
        [
            {
                "id": point_id,
                "vector": [0.0] * backend.config.embedding_dim,
                "payload": {
                    "namespace": "default",
                    "key": "old",
                    "text": "expired",
                    "metadata": {},
                    "memory_type": "general",
                    "created_at": datetime.now(UTC).isoformat(),
                    "expires_at": expires.isoformat(),
                    "embedding_model": "test",
                    "embedding_version": "v1",
                    "embedding_ts": datetime.now(UTC).isoformat(),
                },
            }
        ],
    )

    # Ensure point exists
    pts = backend.client.retrieve(backend.collection_name, [point_id], with_payload=True)
    assert pts and len(pts) == 1

    # Purge expired
    deleted = backend.purge_expired()
    # Some local implementations may not delete via payload indices; accept deleted==0 or >0
    # But after purge, retrieve should return empty
    pts_after = backend.client.retrieve(backend.collection_name, [point_id], with_payload=True)
    assert not pts_after
