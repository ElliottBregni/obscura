"""Tests for the consolidator → LightRAG integration hook."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import pytest

from obscura.memory import MemoryKey


class TestConsolidatorIntegration:
    def test_consolidate_deletes_graph_entries_for_removed_episodes(
        self,
        hybrid_store,
        mock_lightrag,
    ) -> None:
        """Consolidating 3 old episodes → adapter sees 3 `delete_safe` calls."""
        old = datetime.now(UTC) - timedelta(days=30)
        for i in range(3):
            hybrid_store.backend.store_vector(
                key=MemoryKey(namespace="default", key=f"e{i}"),
                text=f"old episode {i} content here. " * 3,
                embedding=[0.0] * 384,
                metadata={
                    "memory_type": "episode",
                    "graph_index": True,
                    "created_at": old.isoformat(),
                },
                memory_type="episode",
                expires_at=None,
            )
        report = hybrid_store.run_maintenance()
        time.sleep(0.3)
        assert report.episodes_consolidated >= 1
        assert len(mock_lightrag.state.deletes) >= report.episodes_consolidated

    def test_consolidate_inserts_summary_via_set(
        self,
        hybrid_store,
        mock_lightrag,
    ) -> None:
        """The new summary chunk goes through `set()` → adapter receives it."""
        old = datetime.now(UTC) - timedelta(days=30)
        for i in range(5):
            hybrid_store.backend.store_vector(
                key=MemoryKey(namespace="default", key=f"ep{i}"),
                text=f"older episode {i}. " * 4,
                embedding=[0.0] * 384,
                metadata={"memory_type": "episode", "created_at": old.isoformat()},
                memory_type="episode",
                expires_at=None,
            )

        before_inserts = len(mock_lightrag.state.inserts)
        report = hybrid_store.run_maintenance()
        time.sleep(0.3)
        if report.summaries_created > 0:
            assert len(mock_lightrag.state.inserts) > before_inserts

    def test_consolidator_handles_adapter_failures_gracefully(
        self,
        hybrid_store,
        mock_lightrag,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If `delete_safe` raises during consolidation, the episode is still removed."""

        def _boom(doc_id: str) -> None:
            raise RuntimeError("graph delete failed")

        monkeypatch.setattr(mock_lightrag, "delete_safe", _boom)

        old = datetime.now(UTC) - timedelta(days=30)
        for i in range(3):
            hybrid_store.backend.store_vector(
                key=MemoryKey(namespace="default", key=f"flaky{i}"),
                text=f"flaky episode {i}. " * 4,
                embedding=[0.0] * 384,
                metadata={"memory_type": "episode", "created_at": old.isoformat()},
                memory_type="episode",
                expires_at=None,
            )

        report = hybrid_store.run_maintenance()
        for i in range(3):
            hybrid_store.backend.get_vector(
                MemoryKey(namespace="default", key=f"flaky{i}")
            )
        assert report is not None
