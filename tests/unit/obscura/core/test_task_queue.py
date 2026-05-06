"""Unit tests for the SQLite-backed TaskQueue, focused on idempotent enqueue."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from obscura.core import task_queue as tq_mod
from obscura.core.enums.lifecycle import TaskQueueStatus
from obscura.core.task_queue import TaskQueue


@pytest.fixture
def isolated_queue(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[TaskQueue]:
    """TaskQueue rooted in a tmp dir so tests don't touch ~/.obscura/tasks.db."""
    db = tmp_path / "tasks.db"
    monkeypatch.setattr(tq_mod, "_db_path", lambda: db)
    yield TaskQueue()


# ---------------------------------------------------------------------------
# derive_dedupe_key
# ---------------------------------------------------------------------------


class TestDeriveDedupeKey:
    def test_normalizes_whitespace_and_case(self) -> None:
        a = TaskQueue.derive_dedupe_key("/repo", "g1", "Fix login bug")
        b = TaskQueue.derive_dedupe_key("/repo", "g1", " Fix  Login Bug ")
        assert a == b

    def test_distinguishes_different_subjects(self) -> None:
        a = TaskQueue.derive_dedupe_key("/repo", "g1", "fix login")
        b = TaskQueue.derive_dedupe_key("/repo", "g1", "fix logout")
        assert a != b

    def test_distinguishes_different_projects(self) -> None:
        a = TaskQueue.derive_dedupe_key("/repo-a", "g1", "fix")
        b = TaskQueue.derive_dedupe_key("/repo-b", "g1", "fix")
        assert a != b

    def test_distinguishes_different_goal_ids(self) -> None:
        a = TaskQueue.derive_dedupe_key("/repo", "g1", "fix")
        b = TaskQueue.derive_dedupe_key("/repo", "g2", "fix")
        assert a != b


# ---------------------------------------------------------------------------
# enqueue idempotency
# ---------------------------------------------------------------------------


class TestEnqueueIdempotency:
    def test_no_dedupe_key_creates_duplicates(self, isolated_queue: TaskQueue) -> None:
        a = isolated_queue.enqueue("Fix login bug")
        b = isolated_queue.enqueue("Fix login bug")
        assert a != b

    def test_same_dedupe_key_returns_existing(self, isolated_queue: TaskQueue) -> None:
        key = TaskQueue.derive_dedupe_key("/repo", "g1", "Fix login bug")
        a = isolated_queue.enqueue("Fix login bug", dedupe_key=key)
        b = isolated_queue.enqueue("Fix login bug", dedupe_key=key)
        assert a == b
        # Only one row should exist.
        depth = isolated_queue.queue_depth(status=TaskQueueStatus.PENDING)
        assert sum(depth.values()) == 1

    def test_different_dedupe_keys_create_separate_tasks(
        self, isolated_queue: TaskQueue
    ) -> None:
        a = isolated_queue.enqueue("subject", dedupe_key="key-a")
        b = isolated_queue.enqueue("subject", dedupe_key="key-b")
        assert a != b

    def test_completed_task_does_not_block_reenqueue(
        self, isolated_queue: TaskQueue
    ) -> None:
        key = "shared-key"
        a = isolated_queue.enqueue("subject", dedupe_key=key)
        # Run it through to completion.
        assert isolated_queue.claim(a, "worker-1") is True
        assert isolated_queue.complete(a, output="done") is True
        # A fresh enqueue with the same key should now create a new row.
        b = isolated_queue.enqueue("subject", dedupe_key=key)
        assert b != a

    def test_failed_task_does_not_block_reenqueue(
        self, isolated_queue: TaskQueue
    ) -> None:
        key = "shared-key"
        a = isolated_queue.enqueue("subject", dedupe_key=key, max_retries=0)
        # Permanent failure (no retry).
        assert isolated_queue.fail(a, "boom", retry=False) is True
        b = isolated_queue.enqueue("subject", dedupe_key=key)
        assert b != a

    def test_pending_retry_still_blocks_reenqueue(
        self, isolated_queue: TaskQueue
    ) -> None:
        """A failed-but-retryable task stays pending and should still
        absorb duplicate enqueues with the same key."""
        key = "shared-key"
        a = isolated_queue.enqueue("subject", dedupe_key=key, max_retries=3)
        assert isolated_queue.claim(a, "worker-1") is True
        assert isolated_queue.fail(a, "transient", retry=True) is True
        # Task is back in pending state with retry_count=1; same key absorbs.
        b = isolated_queue.enqueue("subject", dedupe_key=key)
        assert b == a

    def test_concurrent_enqueue_with_same_key_yields_one_row(
        self, isolated_queue: TaskQueue
    ) -> None:
        """Two threads racing on the same dedupe_key must end up with
        one row, not two — proves the BEGIN IMMEDIATE serialization
        plus the partial unique index hold."""
        key = "race-key"
        results: list[str] = []
        results_lock = threading.Lock()
        barrier = threading.Barrier(8)

        def producer() -> None:
            barrier.wait()
            tid = isolated_queue.enqueue("Race subject", dedupe_key=key)
            with results_lock:
                results.append(tid)

        threads = [threading.Thread(target=producer) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert len(results) == 8
        # All producers got the same task_id.
        assert len(set(results)) == 1
        # And there's only one pending row in the table.
        depth = isolated_queue.queue_depth(status=TaskQueueStatus.PENDING)
        assert sum(depth.values()) == 1
