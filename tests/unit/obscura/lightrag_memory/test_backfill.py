"""Tests for obscura.lightrag_memory.backfill — batch migration of existing chunks.

Covers both the `BackfillEngine` and its Click CLI entrypoint.
"""

from __future__ import annotations

import time
import unittest.mock as _mock
from datetime import UTC, datetime

import pytest
from click.testing import CliRunner

from obscura.memory import MemoryKey


def _seed_corpus(store, count_by_type: dict[str, int]) -> None:
    """Populate the backend with `count_by_type[type]` chunks per memory type."""
    for mtype, count in count_by_type.items():
        for i in range(count):
            store.backend.store_vector(
                key=MemoryKey(namespace="default", key=f"{mtype}_{i}"),
                text=f"{mtype} chunk #{i} content here. " * 3,
                embedding=[0.0] * 384,
                metadata={"memory_type": mtype},
                memory_type=mtype,
                expires_at=None,
            )


class TestBackfillEstimate:
    def test_estimate_counts_only_indexable(self, hybrid_store, mock_lightrag) -> None:
        """60 fact + 30 summary + 10 episode = 90 indexable, 100 total."""
        from obscura.lightrag_memory.backfill import BackfillEngine

        _seed_corpus(hybrid_store, {"fact": 60, "summary": 30, "episode": 10})
        engine = BackfillEngine(store=hybrid_store, adapter=mock_lightrag)
        est = engine.estimate()
        assert est.total_chunks == 100
        assert est.indexable_chunks == 90
        assert est.skipped_chunks == 10

    def test_estimate_excludes_already_indexed(
        self, hybrid_store, mock_lightrag
    ) -> None:
        """Chunks with `lr_indexed_at` set count as 'already done'."""
        from obscura.lightrag_memory.backfill import BackfillEngine

        now = datetime.now(UTC)
        for i in range(5):
            hybrid_store.backend.store_vector(
                key=MemoryKey(namespace="default", key=f"done_{i}"),
                text=f"already done chunk {i}. " * 3,
                embedding=[0.0] * 384,
                metadata={"memory_type": "fact", "lr_indexed_at": now.isoformat()},
                memory_type="fact",
                expires_at=None,
            )
        for i in range(5):
            hybrid_store.backend.store_vector(
                key=MemoryKey(namespace="default", key=f"todo_{i}"),
                text=f"to-do chunk {i}. " * 3,
                embedding=[0.0] * 384,
                metadata={"memory_type": "fact"},
                memory_type="fact",
                expires_at=None,
            )

        engine = BackfillEngine(store=hybrid_store, adapter=mock_lightrag)
        est = engine.estimate()
        assert est.indexable_chunks == 5
        assert est.already_indexed == 5


class TestBackfillExecution:
    def test_dry_run_no_inserts(self, hybrid_store, mock_lightrag) -> None:
        from obscura.lightrag_memory.backfill import BackfillEngine

        _seed_corpus(hybrid_store, {"fact": 10})
        engine = BackfillEngine(store=hybrid_store, adapter=mock_lightrag)
        report = engine.run(dry_run=True)
        assert mock_lightrag.state.inserts == []
        assert report.would_index == 10
        assert report.indexed == 0

    def test_runs_with_max_chunks(self, hybrid_store, mock_lightrag) -> None:
        """`--max-chunks 5` performs exactly 5 inserts."""
        from obscura.lightrag_memory.backfill import BackfillEngine

        _seed_corpus(hybrid_store, {"fact": 20})
        engine = BackfillEngine(store=hybrid_store, adapter=mock_lightrag)
        report = engine.run(max_chunks=5)
        time.sleep(0.5)
        assert len(mock_lightrag.state.inserts) == 5
        assert report.indexed == 5

    def test_idempotent_re_run_skips_indexed(self, hybrid_store, mock_lightrag) -> None:
        """Running twice doesn't re-index already-flagged chunks."""
        from obscura.lightrag_memory.backfill import BackfillEngine

        _seed_corpus(hybrid_store, {"fact": 10})
        engine = BackfillEngine(store=hybrid_store, adapter=mock_lightrag)
        engine.run()
        time.sleep(0.5)
        first_count = len(mock_lightrag.state.inserts)
        assert first_count == 10
        report2 = engine.run()
        time.sleep(0.5)
        assert len(mock_lightrag.state.inserts) == first_count
        assert report2.indexed == 0

    def test_marks_indexed_at_on_success(self, hybrid_store, mock_lightrag) -> None:
        from obscura.lightrag_memory.backfill import BackfillEngine

        _seed_corpus(hybrid_store, {"fact": 3})
        engine = BackfillEngine(store=hybrid_store, adapter=mock_lightrag)
        engine.run()
        time.sleep(0.5)
        for i in range(3):
            entry = hybrid_store.backend.get_vector(
                MemoryKey(namespace="default", key=f"fact_{i}")
            )
            assert entry is not None
            assert "lr_indexed_at" in entry.metadata
            datetime.fromisoformat(entry.metadata["lr_indexed_at"])

    def test_increments_attempts_on_failure(self, hybrid_store, mock_lightrag) -> None:
        """Adapter failures bump `lr_index_attempts` and DO NOT set `lr_indexed_at`."""
        from obscura.lightrag_memory.backfill import BackfillEngine

        _seed_corpus(hybrid_store, {"fact": 2})

        def _fail(doc_id: str, text: str, metadata: dict) -> None:
            raise RuntimeError("simulated")

        with _mock.patch.object(mock_lightrag, "insert_safe", _fail):
            engine = BackfillEngine(store=hybrid_store, adapter=mock_lightrag)
            try:
                engine.run()
            except Exception:
                pass

        time.sleep(0.5)
        for i in range(2):
            entry = hybrid_store.backend.get_vector(
                MemoryKey(namespace="default", key=f"fact_{i}")
            )
            assert entry is not None
            assert entry.metadata.get("lr_index_attempts", 0) >= 1
            assert "lr_indexed_at" not in entry.metadata

    def test_excludes_failed_after_max_attempts(
        self, hybrid_store, mock_lightrag
    ) -> None:
        """Chunks with `lr_index_attempts >= 3` are filtered from the next run."""
        from obscura.lightrag_memory.backfill import BackfillEngine

        for i, attempts in enumerate([4, 0, 0]):
            md: dict = {"memory_type": "fact"}
            if attempts:
                md["lr_index_attempts"] = attempts
            hybrid_store.backend.store_vector(
                key=MemoryKey(namespace="default", key=f"f{i}"),
                text=f"chunk {i} content. " * 3,
                embedding=[0.0] * 384,
                metadata=md,
                memory_type="fact",
                expires_at=None,
            )
        engine = BackfillEngine(store=hybrid_store, adapter=mock_lightrag)
        report = engine.run()
        time.sleep(0.3)
        assert report.indexed == 2

    def test_progress_callback_invoked(self, hybrid_store, mock_lightrag) -> None:
        """`on_progress` is called for each chunk with the running counters."""
        from obscura.lightrag_memory.backfill import BackfillEngine

        _seed_corpus(hybrid_store, {"fact": 5})
        progress_calls: list[tuple[int, int]] = []

        def _on_progress(done: int, total: int) -> None:
            progress_calls.append((done, total))

        engine = BackfillEngine(
            store=hybrid_store,
            adapter=mock_lightrag,
            on_progress=_on_progress,
        )
        engine.run()
        time.sleep(0.3)
        assert len(progress_calls) == 5
        assert progress_calls[-1] == (5, 5)


class TestBackfillFileLock:
    def test_filelock_blocks_concurrent_runs(
        self,
        hybrid_store,
        mock_lightrag,
        tmp_path,
    ) -> None:
        """A second backfill while the first holds the lock fails fast."""
        from obscura.lightrag_memory.backfill import (
            BackfillEngine,
            BackfillLockHeld,
        )

        _seed_corpus(hybrid_store, {"fact": 3})
        engine_a = BackfillEngine(
            store=hybrid_store,
            adapter=mock_lightrag,
            lock_path=tmp_path / "bf.lock",
        )

        with engine_a.acquire_lock():
            engine_b = BackfillEngine(
                store=hybrid_store,
                adapter=mock_lightrag,
                lock_path=tmp_path / "bf.lock",
            )
            with pytest.raises(BackfillLockHeld):
                engine_b.run()


class TestBackfillCLI:
    def test_dry_run_prints_estimate(
        self,
        hybrid_store,
        mock_lightrag,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`--dry-run` prints chunk counts but performs no inserts."""
        from obscura.lightrag_memory import cli as cli_mod
        from obscura.lightrag_memory.cli import backfill_graph_cmd

        monkeypatch.setattr(cli_mod, "_resolve_store", lambda user_id: hybrid_store)
        monkeypatch.setattr(cli_mod, "_resolve_adapter", lambda store: mock_lightrag)

        for i in range(5):
            hybrid_store.backend.store_vector(
                key=MemoryKey(namespace="default", key=f"f{i}"),
                text=f"chunk {i} content. " * 3,
                embedding=[0.0] * 384,
                metadata={"memory_type": "fact"},
                memory_type="fact",
                expires_at=None,
            )

        runner = CliRunner()
        result = runner.invoke(backfill_graph_cmd, ["--dry-run"])
        assert result.exit_code == 0, result.output
        assert "5" in result.output
        assert (
            "indexable" in result.output.lower()
            or "would index" in result.output.lower()
        )
        assert mock_lightrag.state.inserts == []

    def test_confirm_required_above_threshold(
        self,
        hybrid_store,
        mock_lightrag,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """High estimated cost → exits non-zero without `--confirm`."""
        from obscura.lightrag_memory import cli as cli_mod
        from obscura.lightrag_memory.cli import backfill_graph_cmd

        monkeypatch.setattr(cli_mod, "_resolve_store", lambda user_id: hybrid_store)
        monkeypatch.setattr(cli_mod, "_resolve_adapter", lambda store: mock_lightrag)

        for i in range(1500):
            hybrid_store.backend.store_vector(
                key=MemoryKey(namespace="default", key=f"f{i}"),
                text=f"chunk {i}. " * 3,
                embedding=[0.0] * 384,
                metadata={"memory_type": "fact"},
                memory_type="fact",
                expires_at=None,
            )

        runner = CliRunner()
        result = runner.invoke(backfill_graph_cmd, [])
        assert result.exit_code != 0
        assert "confirm" in result.output.lower()
        assert mock_lightrag.state.inserts == []

    def test_max_chunks_passed_through(
        self,
        hybrid_store,
        mock_lightrag,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`--max-chunks 3` performs exactly 3 inserts."""
        from obscura.lightrag_memory import cli as cli_mod
        from obscura.lightrag_memory.cli import backfill_graph_cmd

        monkeypatch.setattr(cli_mod, "_resolve_store", lambda user_id: hybrid_store)
        monkeypatch.setattr(cli_mod, "_resolve_adapter", lambda store: mock_lightrag)

        for i in range(10):
            hybrid_store.backend.store_vector(
                key=MemoryKey(namespace="default", key=f"f{i}"),
                text=f"chunk {i} content. " * 3,
                embedding=[0.0] * 384,
                metadata={"memory_type": "fact"},
                memory_type="fact",
                expires_at=None,
            )

        runner = CliRunner()
        result = runner.invoke(backfill_graph_cmd, ["--max-chunks", "3", "--confirm"])
        assert result.exit_code == 0, result.output
        time.sleep(0.5)
        assert len(mock_lightrag.state.inserts) == 3
