"""Integration tests for :class:`PostgresTaskRepo` against a real Postgres."""

from __future__ import annotations

import time
from typing import Any

import pytest

from obscura.data.tasks.postgres import PostgresTaskRepo


pytestmark = pytest.mark.integration


class TestPostgresTaskRepoLifecycle:
    def test_enqueue_and_get(self, pg_env: dict[str, Any]) -> None:
        del pg_env
        repo = PostgresTaskRepo()
        tid = repo.enqueue(
            "ship the data layer",
            description="run the migration",
            priority=10,
            metadata={"project": "obscura"},
        )
        task = repo.get(tid)
        assert task is not None
        assert task["task_id"] == tid
        assert task["subject"] == "ship the data layer"
        assert task["priority"] == 10
        assert task["metadata"] == {"project": "obscura"}
        assert task["status"] == "pending"

    def test_get_missing_returns_none(self, pg_env: dict[str, Any]) -> None:
        del pg_env
        repo = PostgresTaskRepo()
        assert repo.get("does-not-exist") is None

    def test_priority_ordering(self, pg_env: dict[str, Any]) -> None:
        del pg_env
        repo = PostgresTaskRepo()
        repo.enqueue("low", priority=80)
        high_id = repo.enqueue("high", priority=10)
        repo.enqueue("medium", priority=50)

        ready = repo.next_ready()
        assert ready is not None
        assert ready["task_id"] == high_id

    def test_claim_and_release(self, pg_env: dict[str, Any]) -> None:
        del pg_env
        repo = PostgresTaskRepo()
        tid = repo.enqueue("claim me")

        # First claim wins
        assert repo.claim(tid, "worker-1") is True
        # Second worker can't steal it
        assert repo.claim(tid, "worker-2") is False

        # Release frees it
        assert repo.release(tid, "worker-1") is True
        assert repo.claim(tid, "worker-2") is True

    def test_release_only_by_owner(self, pg_env: dict[str, Any]) -> None:
        del pg_env
        repo = PostgresTaskRepo()
        tid = repo.enqueue("hands off")
        repo.claim(tid, "worker-1")
        # worker-2 can't release worker-1's claim
        assert repo.release(tid, "worker-2") is False
        # worker-1 still owns it
        assert repo.claim(tid, "worker-3") is False

    def test_heartbeat_extends_claim(self, pg_env: dict[str, Any]) -> None:
        del pg_env
        # tight timeout to speed up the staleness test
        repo = PostgresTaskRepo(claim_timeout=0.5)
        tid = repo.enqueue("long-running")
        repo.claim(tid, "worker-1")

        time.sleep(0.6)  # claim now stale
        # Without heartbeat, another worker can steal it
        assert repo.claim(tid, "worker-2") is True

        # Reset and test that heartbeat refreshes it
        repo.release(tid, "worker-2")
        repo.claim(tid, "worker-1")
        time.sleep(0.3)
        assert repo.heartbeat(tid, "worker-1") is True
        # Still not stale enough to steal
        assert repo.claim(tid, "worker-2") is False

    def test_heartbeat_only_by_owner(self, pg_env: dict[str, Any]) -> None:
        del pg_env
        repo = PostgresTaskRepo()
        tid = repo.enqueue("auth-check")
        repo.claim(tid, "worker-1")
        assert repo.heartbeat(tid, "worker-2") is False

    def test_complete(self, pg_env: dict[str, Any]) -> None:
        del pg_env
        repo = PostgresTaskRepo()
        tid = repo.enqueue("done quickly")
        repo.claim(tid, "worker-1")
        assert repo.complete(tid, output="all green") is True
        task = repo.get(tid)
        assert task is not None
        assert task["status"] == "completed"
        assert task["output"] == "all green"
        assert task["claimed_by"] == ""

    def test_fail_with_retry(self, pg_env: dict[str, Any]) -> None:
        del pg_env
        repo = PostgresTaskRepo()
        tid = repo.enqueue("flaky", max_retries=2)
        repo.claim(tid, "worker-1")
        assert repo.fail(tid, "transient error", retry=True) is True

        task = repo.get(tid)
        assert task is not None
        assert task["status"] == "pending"  # requeued for retry
        assert task["retry_count"] == 1
        assert task["error"] == "transient error"

    def test_fail_exhausts_retries(self, pg_env: dict[str, Any]) -> None:
        del pg_env
        repo = PostgresTaskRepo()
        tid = repo.enqueue("doomed", max_retries=1)
        repo.claim(tid, "w")
        repo.fail(tid, "1st", retry=True)  # → pending, retry_count=1
        # Force run_after to past so we can re-claim immediately
        # (avoid sleep-based test); for this test we just check state.
        task = repo.get(tid)
        assert task["retry_count"] == 1

        # Simulate next attempt failing
        repo.claim(tid, "w")
        repo.fail(tid, "2nd", retry=True)  # already at max → permanent
        task = repo.get(tid)
        assert task["status"] == "failed"
        assert task["error"] == "2nd"

    def test_fail_no_retry(self, pg_env: dict[str, Any]) -> None:
        del pg_env
        repo = PostgresTaskRepo()
        tid = repo.enqueue("permanent")
        repo.claim(tid, "w")
        repo.fail(tid, "boom", retry=False)
        task = repo.get(tid)
        assert task["status"] == "failed"

    def test_dependency_blocking(self, pg_env: dict[str, Any]) -> None:
        del pg_env
        repo = PostgresTaskRepo()
        first = repo.enqueue("step 1", priority=10)
        repo.enqueue("step 2", priority=10, blocked_by=[first])

        # next_ready should return step 1, not step 2 (blocked)
        ready = repo.next_ready()
        assert ready is not None
        assert ready["task_id"] == first

        # Complete step 1 and step 2 becomes ready
        repo.claim(first, "w")
        repo.complete(first)
        ready = repo.next_ready()
        assert ready is not None
        assert ready["subject"] == "step 2"

    def test_run_after_delays_dequeue(self, pg_env: dict[str, Any]) -> None:
        del pg_env
        repo = PostgresTaskRepo()
        future = time.time() + 60.0
        repo.enqueue("not yet", run_after=future)
        ready = repo.next_ready()
        assert ready is None

    def test_project_root_filter(self, pg_env: dict[str, Any]) -> None:
        del pg_env
        repo = PostgresTaskRepo()
        repo.enqueue("alpha", project_root="/proj/alpha")
        repo.enqueue("beta", project_root="/proj/beta")

        a_ready = repo.next_ready(project_root="/proj/alpha")
        assert a_ready is not None
        assert a_ready["subject"] == "alpha"

        b_ready = repo.next_ready(project_root="/proj/beta")
        assert b_ready is not None
        assert b_ready["subject"] == "beta"

    def test_queue_depth(self, pg_env: dict[str, Any]) -> None:
        del pg_env
        repo = PostgresTaskRepo()
        for _ in range(3):
            repo.enqueue("a", priority=10)
        for _ in range(2):
            repo.enqueue("b", priority=50)
        depth = repo.queue_depth()
        assert depth == {"10": 3, "50": 2}

    def test_list_claimed(self, pg_env: dict[str, Any]) -> None:
        del pg_env
        repo = PostgresTaskRepo()
        a = repo.enqueue("a")
        b = repo.enqueue("b")
        repo.enqueue("c")  # unclaimed
        repo.claim(a, "worker-1")
        repo.claim(b, "worker-1")
        claimed = repo.list_claimed("worker-1")
        assert {t["task_id"] for t in claimed} == {a, b}

    def test_reclaim_stale(self, pg_env: dict[str, Any]) -> None:
        del pg_env
        repo = PostgresTaskRepo(claim_timeout=0.2)
        a = repo.enqueue("alpha")
        repo.claim(a, "ghost-worker")
        time.sleep(0.3)

        released = repo.reclaim_stale()
        assert released == 1
        # Task is now claimable again
        assert repo.claim(a, "fresh-worker") is True

    def test_factory_picks_postgres_when_env_set(
        self,
        pg_env: dict[str, Any],
    ) -> None:
        del pg_env
        from obscura.data.tasks.factory import get_task_repo
        from obscura.data.tasks.postgres import (
            PostgresTaskRepo as _PTR,
        )

        repo = get_task_repo()
        assert isinstance(repo, _PTR)
