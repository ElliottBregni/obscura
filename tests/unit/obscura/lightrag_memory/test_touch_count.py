"""Tests for `_touch_and_count_async` — the usage-frequency / lazy-index path."""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime

from obscura.memory import MemoryKey


class TestTouchAtomicity:
    def test_single_touch_increments(self, hybrid_store, mock_lightrag) -> None:
        hybrid_store.set("k1", "content " * 10, memory_type="fact")
        entry_before = hybrid_store.get("k1")
        assert entry_before is not None
        before = entry_before.metadata.get("access_count", 0)

        hybrid_store.touch("k1")
        for _ in range(20):
            entry = hybrid_store.get("k1")
            if entry and entry.metadata.get("access_count", 0) > before:
                break
            time.sleep(0.05)

        entry = hybrid_store.get("k1")
        assert entry is not None
        assert entry.metadata.get("access_count", 0) == before + 1

    def test_concurrent_touches_relaxed(self, hybrid_store, mock_lightrag) -> None:
        """10 concurrent touches → final count in [1, 10].

        Racy increments are an acceptable failure mode for usage stats; pin
        higher only if the implementation uses atomic SQL upserts.
        """
        hybrid_store.set("k1", "raced content " * 5, memory_type="fact")

        def _touch() -> None:
            hybrid_store.touch("k1")

        threads = [threading.Thread(target=_touch) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        time.sleep(0.5)

        entry = hybrid_store.get("k1")
        assert entry is not None
        count = entry.metadata.get("access_count", 0)
        assert 1 <= count <= 10, f"unexpected count: {count}"

    def test_touch_missing_key_no_error(self, hybrid_store, mock_lightrag) -> None:
        """Touching a nonexistent key is a silent no-op."""
        hybrid_store.touch("phantom-key")


class TestLazyIndex:
    def test_touch_schedules_lazy_ingest_when_unindexed(
        self,
        hybrid_store,
        mock_lightrag,
    ) -> None:
        """A chunk touched but not yet graph-indexed → schedule ingest."""
        hybrid_store.backend.store_vector(
            key=MemoryKey(namespace="default", key="legacy"),
            text="legacy content here. " * 5,
            embedding=[0.0] * 384,
            metadata={"memory_type": "fact"},
            memory_type="fact",
            expires_at=None,
        )
        assert mock_lightrag.state.inserts == []

        hybrid_store.touch("legacy")

        for _ in range(40):
            if len(mock_lightrag.state.inserts) >= 1:
                break
            time.sleep(0.05)
        assert len(mock_lightrag.state.inserts) >= 1, "lazy ingest never fired"

    def test_touch_skips_already_indexed(
        self,
        hybrid_store,
        mock_lightrag,
    ) -> None:
        """A chunk with `lr_indexed_at` set → no re-ingest on touch."""
        now = datetime.now(UTC)
        hybrid_store.backend.store_vector(
            key=MemoryKey(namespace="default", key="indexed"),
            text="indexed content. " * 5,
            embedding=[0.0] * 384,
            metadata={"memory_type": "fact", "lr_indexed_at": now.isoformat()},
            memory_type="fact",
            expires_at=None,
        )
        assert mock_lightrag.state.inserts == []
        hybrid_store.touch("indexed")
        time.sleep(0.2)
        assert mock_lightrag.state.inserts == []

    def test_touch_respects_attempt_limit(
        self,
        hybrid_store,
        mock_lightrag,
    ) -> None:
        """A chunk with `lr_index_attempts >= 3` → no lazy ingest."""
        hybrid_store.backend.store_vector(
            key=MemoryKey(namespace="default", key="poisoned"),
            text="content that keeps failing. " * 5,
            embedding=[0.0] * 384,
            metadata={"memory_type": "fact", "lr_index_attempts": 4},
            memory_type="fact",
            expires_at=None,
        )
        hybrid_store.touch("poisoned")
        time.sleep(0.2)
        assert mock_lightrag.state.inserts == []
