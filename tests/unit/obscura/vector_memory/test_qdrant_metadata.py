from __future__ import annotations

from obscura.auth.models import AuthenticatedUser
from obscura.vector_memory import VectorMemoryStore
from obscura.vector_memory.backends import BackendConfig
from obscura.vector_memory.backends.qdrant_backend import QdrantBackend, _point_id


def test_qdrant_embedding_metadata() -> None:
    """Ensure embedding provenance fields are present in Qdrant payload."""
    # Create a lightweight AuthenticatedUser for this test (no test fixture required)
    test_user = AuthenticatedUser(
        user_id="u-qdrant-meta-test",
        email="qdrant@tests.local",
        roles=("tester",),
        org_id="org-test",
        token_type="user",
        raw_token="test",
    )

    config = BackendConfig(user_id=test_user.user_id, embedding_dim=384)
    backend = QdrantBackend(config=config, mode="memory")
    store = VectorMemoryStore(test_user, backend=backend)

    # Store a memory using default namespace
    store.set("meta-key", "some important text", metadata={"tag": "meta-test"})

    # Compute deterministic point id (same logic as backend)
    point_id = _point_id("default", "meta-key")

    # Retrieve raw point from the Qdrant client
    points = backend.client.retrieve(backend.collection_name, [point_id], with_payload=True, with_vectors=False)
    assert points, "Expected point to be present in Qdrant memory"
    p = points[0]

    # Embedding provenance fields should exist
    assert "embedding_model" in p.payload
    assert "embedding_version" in p.payload
    assert "embedding_ts" in p.payload

    # embedding_ts should parse as ISO-ish string
    assert isinstance(p.payload["embedding_ts"], str) and len(p.payload["embedding_ts"]) > 0
