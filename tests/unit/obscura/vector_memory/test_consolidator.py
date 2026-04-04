"""Tests for obscura.vector_memory.consolidator — episode consolidation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from obscura.auth.models import AuthenticatedUser
from obscura.vector_memory.backends.sqlite_backend import SQLiteBackend
from obscura.vector_memory.consolidator import MemoryConsolidator
from obscura.vector_memory.decay import DecayConfig
from obscura.vector_memory.vector_memory import VectorMemoryStore


def _make_store(tmp_path):
    """Create a test VectorMemoryStore with SQLite backend in tmp_path."""
    from obscura.vector_memory.backends.base import BackendConfig

    user = AuthenticatedUser(
        user_id="u-consolidator-test",
        email="test@test.local",
        roles=("tester",),
        org_id="org-test",
        token_type="user",
        raw_token="test",
    )
    config = BackendConfig(user_id=user.user_id, embedding_dim=8)
    decay_config = DecayConfig(consolidation_age_days=7)
    backend = SQLiteBackend(
        config=config,
        db_path=tmp_path / "test.db",
        decay_config=decay_config,
    )

    def embedding_fn(text):
        return [0.0] * 8

    return VectorMemoryStore(
        user,
        backend=backend,
        embedding_fn=embedding_fn,
        decay_config=decay_config,
    )


def test_consolidate_skips_recent(tmp_path) -> None:
    """Episodes younger than consolidation_age_days should be skipped."""
    store = _make_store(tmp_path)

    # Add recent episodes
    for i in range(5):
        store.set(
            key=f"recent_{i}",
            text=f"Recent turn {i}",
            metadata={"session_id": "recent_session", "turn": i},
            namespace="cli:conversation",
            memory_type="episode",
        )

    consolidator = MemoryConsolidator(
        store=store,
        config=store.decay_config,
        summarize_fn=lambda texts: "SUMMARY",
    )
    deleted, created = consolidator.consolidate()

    assert deleted == 0
    assert created == 0


