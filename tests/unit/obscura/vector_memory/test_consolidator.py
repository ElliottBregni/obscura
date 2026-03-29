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
    backend = SQLiteBackend(config=config, db_path=tmp_path / "test.db", decay_config=decay_config)
    embedding_fn = lambda text: [0.0] * 8
    return VectorMemoryStore(user, backend=backend, embedding_fn=embedding_fn, decay_config=decay_config)


def test_consolidate_groups_by_session(tmp_path):
    """Episodes should be grouped by session_id and consolidated."""
    store = _make_store(tmp_path)
    now = datetime.now(UTC)
    old = now - timedelta(days=10)

    # Add 4 episodes for session A (above threshold of 3)
    for i in range(4):
        store.set(
            key=f"ep_a_{i}",
            text=f"User asked about topic {i}. Assistant explained it.",
            metadata={"session_id": "session_a", "turn": i},
            namespace="cli:conversation",
            memory_type="episode",
        )
        # Backdate the created_at to make them old
        store.backend._get_conn().execute(
            "UPDATE vector_memory SET created_at = ? WHERE key = ?",
            (old.isoformat(), f"ep_a_{i}"),
        )
        store.backend._get_conn().commit()

    # Add 2 episodes for session B (below threshold)
    for i in range(2):
        store.set(
            key=f"ep_b_{i}",
            text=f"Session B turn {i}",
            metadata={"session_id": "session_b", "turn": i},
            namespace="cli:conversation",
            memory_type="episode",
        )
        store.backend._get_conn().execute(
            "UPDATE vector_memory SET created_at = ? WHERE key = ?",
            (old.isoformat(), f"ep_b_{i}"),
        )
        store.backend._get_conn().commit()

    # Use simple fallback summarizer
    consolidator = MemoryConsolidator(
        store=store,
        config=store.decay_config,
        summarize_fn=lambda texts: "SUMMARY: " + " | ".join(texts[:2]),
    )
    deleted, created = consolidator.consolidate()

    # Session A should be consolidated (4 episodes → 1 summary)
    assert created == 1
    assert deleted == 4

    # Session B should be untouched (only 2 episodes)
    keys = store.list_keys()
    key_names = [k.key for k in keys]
    assert "ep_b_0" in key_names
    assert "ep_b_1" in key_names

    # Check the summary was created
    summaries = [k for k in keys if k.key.startswith("summary_")]
    assert len(summaries) == 1


def test_consolidate_skips_recent(tmp_path):
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


def test_consolidate_preserves_metadata(tmp_path):
    """The summary should inherit metadata from the first episode."""
    store = _make_store(tmp_path)
    old = datetime.now(UTC) - timedelta(days=10)

    for i in range(3):
        store.set(
            key=f"meta_ep_{i}",
            text=f"Turn {i} discussion",
            metadata={"session_id": "meta_session", "turn": i, "custom_tag": "important"},
            namespace="cli:conversation",
            memory_type="episode",
        )
        store.backend._get_conn().execute(
            "UPDATE vector_memory SET created_at = ? WHERE key = ?",
            (old.isoformat(), f"meta_ep_{i}"),
        )
        store.backend._get_conn().commit()

    consolidator = MemoryConsolidator(
        store=store,
        config=store.decay_config,
        summarize_fn=lambda texts: "Consolidated summary",
    )
    deleted, created = consolidator.consolidate()

    assert created == 1
    # Find the summary
    keys = store.list_keys()
    summary_key = [k for k in keys if k.key.startswith("summary_")][0]
    entry = store.get(summary_key)
    assert entry is not None
    assert entry.metadata["original_session_id"] == "meta_session"
    assert entry.metadata["consolidated_from"] == 3
