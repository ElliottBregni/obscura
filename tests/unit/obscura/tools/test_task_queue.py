"""Unit tests for obscura.core.task_queue."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from obscura.core.task_queue import TaskQueue


@pytest.fixture()
def q(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TaskQueue:
    """Return a TaskQueue backed by a temp DB (not ~/.obscura/tasks.db)."""
    db_file = tmp_path / "tasks.db"
    monkeypatch.setattr(
        "obscura.core.task_queue._db_path",
        lambda: db_file,
    )
    return TaskQueue(claim_timeout=10.0)


# ---------------------------------------------------------------------------
# enqueue / next_ready
# ---------------------------------------------------------------------------


def test_enqueue_and_dequeue(q: TaskQueue) -> None:
    tid = q.enqueue("Do the thing", description="step 1")
    task = q.next_ready()
    assert task is not None
    assert task["task_id"] == tid
    assert task["subject"] == "Do the thing"


def test_priority_ordering(q: TaskQueue) -> None:
    low = q.enqueue("Low priority", priority=75)
    crit = q.enqueue("Critical", priority=0)
    med = q.enqueue("Medium", priority=50)

    first = q.next_ready()
    assert first is not None
    assert first["task_id"] == crit  # 0 = critical, should be first


def test_empty_queue_returns_none(q: TaskQueue) -> None:
    assert q.next_ready() is None


def test_run_after_hides_task(q: TaskQueue) -> None:
    future = time.time() + 3600
    q.enqueue("Scheduled", run_after=future)
    assert q.next_ready() is None


def test_run_after_past_is_visible(q: TaskQueue) -> None:
    past = time.time() - 1
    tid = q.enqueue("Past-scheduled", run_after=past)
    task = q.next_ready()
    assert task is not None
    assert task["task_id"] == tid


# ---------------------------------------------------------------------------
# claim / release
# ---------------------------------------------------------------------------


def test_claim_returns_true_on_success(q: TaskQueue) -> None:
    tid = q.enqueue("Claim me")
    task = q.next_ready()
    assert task is not None
    assert q.claim(tid, "worker-1")


def test_double_claim_returns_false(q: TaskQueue) -> None:
    tid = q.enqueue("Claim me")
    assert q.claim(tid, "worker-1")
    assert not q.claim(tid, "worker-2")


def test_claimed_task_hidden_from_next_ready(q: TaskQueue) -> None:
    tid = q.enqueue("Claim me")
    assert q.claim(tid, "worker-1")
    # Should not be returned to another worker.
    assert q.next_ready(worker_id="worker-2") is None


def test_release_makes_task_available_again(q: TaskQueue) -> None:
    tid = q.enqueue("Release me")
    q.claim(tid, "worker-1")
    q.release(tid, "worker-1")
    task = q.next_ready()
    assert task is not None
    assert task["task_id"] == tid


def test_stale_claim_reclaimed(q: TaskQueue) -> None:
    # Use a very short claim timeout.
    q2 = TaskQueue(claim_timeout=0.001)
    tid = q2.enqueue("Stale")
    assert q2.claim(tid, "worker-1")
    time.sleep(0.01)  # Wait for claim to go stale.
    # next_ready should see it again.
    task = q2.next_ready()
    assert task is not None
    assert task["task_id"] == tid


# ---------------------------------------------------------------------------
# heartbeat
# ---------------------------------------------------------------------------


def test_heartbeat_keeps_claim_alive(q: TaskQueue) -> None:
    tid = q.enqueue("Heartbeat test")
    q.claim(tid, "worker-1")
    assert q.heartbeat(tid, "worker-1")
    # Still claimed by worker-1.
    assert not q.claim(tid, "worker-2")


def test_heartbeat_wrong_worker_returns_false(q: TaskQueue) -> None:
    tid = q.enqueue("Heartbeat test")
    q.claim(tid, "worker-1")
    assert not q.heartbeat(tid, "worker-2")


# ---------------------------------------------------------------------------
# complete
# ---------------------------------------------------------------------------


def test_complete_marks_done(q: TaskQueue) -> None:
    tid = q.enqueue("Complete me")
    q.claim(tid, "worker-1")
    assert q.complete(tid, output="done!")
    task = q.get(tid)
    assert task is not None
    assert task["status"] == "completed"
    assert task["output"] == "done!"
    assert task["claimed_by"] == ""


def test_completed_task_not_in_queue(q: TaskQueue) -> None:
    tid = q.enqueue("Complete me")
    q.claim(tid, "worker-1")
    q.complete(tid)
    assert q.next_ready() is None


# ---------------------------------------------------------------------------
# fail / retry
# ---------------------------------------------------------------------------


def test_fail_with_retries_requeues(q: TaskQueue) -> None:
    tid = q.enqueue("Flaky task", max_retries=2)
    q.claim(tid, "w")
    q.fail(tid, "timeout", retry=True)
    task = q.get(tid)
    assert task is not None
    assert task["status"] == "pending"
    assert task["retry_count"] == 1
    assert task["run_after"] > time.time()  # backoff applied


def test_fail_exhausted_retries_marks_failed(q: TaskQueue) -> None:
    tid = q.enqueue("Doomed task", max_retries=1)
    q.claim(tid, "w")
    q.fail(tid, "err1", retry=True)  # retry_count=1, still < max (wait for backoff)
    # Manually reset run_after so we can claim again.
    import sqlite3
    from obscura.core.task_queue import _db_path
    conn = sqlite3.connect(str(_db_path()))
    conn.execute("UPDATE tasks SET run_after = 0 WHERE task_id = ?", (tid,))
    conn.commit()
    conn.close()
    task = q.next_ready()
    assert task is not None
    q.claim(tid, "w")
    q.fail(tid, "err2", retry=True)  # retry_count=1 >= max_retries=1 → permanent fail
    task = q.get(tid)
    assert task is not None
    assert task["status"] == "failed"


def test_fail_no_retry(q: TaskQueue) -> None:
    tid = q.enqueue("No retry", max_retries=3)
    q.claim(tid, "w")
    q.fail(tid, "fatal", retry=False)
    task = q.get(tid)
    assert task is not None
    assert task["status"] == "failed"


# ---------------------------------------------------------------------------
# dependency gating
# ---------------------------------------------------------------------------


def test_blocked_task_not_dequeued(q: TaskQueue) -> None:
    dep = q.enqueue("Dependency")
    child = q.enqueue("Child", blocked_by=[dep])
    # Only the dep should be returned.
    task = q.next_ready()
    assert task is not None
    assert task["task_id"] == dep
    # Claim dep so it's hidden, child still blocked.
    q.claim(dep, "w")
    assert q.next_ready() is None


def test_blocked_task_dequeued_after_dep_completes(q: TaskQueue) -> None:
    dep = q.enqueue("Dep")
    child = q.enqueue("Child", blocked_by=[dep])
    q.claim(dep, "w")
    q.complete(dep)
    task = q.next_ready()
    assert task is not None
    assert task["task_id"] == child


# ---------------------------------------------------------------------------
# queue_depth / reclaim_stale
# ---------------------------------------------------------------------------


def test_queue_depth(q: TaskQueue) -> None:
    q.enqueue("A", priority=0)
    q.enqueue("B", priority=50)
    q.enqueue("C", priority=50)
    depth = q.queue_depth()
    assert depth.get("0") == 1
    assert depth.get("50") == 2


def test_reclaim_stale(q: TaskQueue) -> None:
    q2 = TaskQueue(claim_timeout=0.001)
    tid = q2.enqueue("Stale")
    q2.claim(tid, "w")
    time.sleep(0.01)
    released = q2.reclaim_stale()
    assert released == 1
    task = q2.get(tid)
    assert task is not None
    assert task["claimed_by"] == ""


# ---------------------------------------------------------------------------
# goal_id passthrough
# ---------------------------------------------------------------------------


def test_goal_id_stored(q: TaskQueue) -> None:
    tid = q.enqueue("Goal task", goal_id="my-goal-123")
    task = q.get(tid)
    assert task is not None
    assert task["goal_id"] == "my-goal-123"


# ---------------------------------------------------------------------------
# list_claimed
# ---------------------------------------------------------------------------


def test_list_claimed(q: TaskQueue) -> None:
    tid = q.enqueue("Claim me")
    q.claim(tid, "worker-1")
    claimed = q.list_claimed("worker-1")
    assert len(claimed) == 1
    assert claimed[0]["task_id"] == tid
    assert q.list_claimed("worker-2") == []


# ---------------------------------------------------------------------------
# get on missing ID
# ---------------------------------------------------------------------------


def test_get_missing_returns_none(q: TaskQueue) -> None:
    assert q.get("nonexistent") is None


# ---------------------------------------------------------------------------
# Goal auto-decomposition (Phase 3)
# ---------------------------------------------------------------------------


def test_goal_auto_decompose(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When a goal transitions to in_progress with criteria and no tasks,
    acceptance criteria are pushed into the task queue."""
    from obscura.kairos.goals import GoalBoard

    db_file = tmp_path / "tasks.db"
    monkeypatch.setattr("obscura.core.task_queue._db_path", lambda: db_file)

    goals_dir = tmp_path / "goals"
    board = GoalBoard(goals_dir=goals_dir)

    goal = board.create(
        "Ship auth",
        priority="high",
        acceptance_criteria=["SSO login works", "Tests pass", "Docs updated"],
    )
    assert goal.status == "active"
    assert not goal.tasks  # No tasks yet.

    # Transition to in_progress — should auto-decompose.
    updated = board.update(goal.id, status="in_progress")
    assert updated is not None
    assert len(updated.tasks) == 3

    # Verify tasks exist in the queue with correct goal_id and priority.
    q = TaskQueue()
    for tid in updated.tasks:
        task = q.get(tid)
        assert task is not None
        assert task["goal_id"] == goal.id
        assert task["priority"] == 25  # high → priority_rank=1 → 1*25=25

    # Verify dependency chain: each task blocked by the previous.
    t0 = q.get(updated.tasks[0])
    t1 = q.get(updated.tasks[1])
    t2 = q.get(updated.tasks[2])
    assert t0 is not None and t0["blocked_by"] == []
    assert t1 is not None and t1["blocked_by"] == [updated.tasks[0]]
    assert t2 is not None and t2["blocked_by"] == [updated.tasks[1]]


def test_goal_no_decompose_without_criteria(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Goals without acceptance criteria should NOT auto-decompose."""
    from obscura.kairos.goals import GoalBoard

    db_file = tmp_path / "tasks.db"
    monkeypatch.setattr("obscura.core.task_queue._db_path", lambda: db_file)

    board = GoalBoard(goals_dir=tmp_path / "goals")
    goal = board.create("Quick fix", priority="low")
    updated = board.update(goal.id, status="in_progress")
    assert updated is not None
    assert not updated.tasks


def test_goal_no_decompose_if_already_has_tasks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Goals that already have linked tasks should NOT auto-decompose again."""
    from obscura.kairos.goals import GoalBoard

    db_file = tmp_path / "tasks.db"
    monkeypatch.setattr("obscura.core.task_queue._db_path", lambda: db_file)

    board = GoalBoard(goals_dir=tmp_path / "goals")
    goal = board.create(
        "Existing tasks",
        acceptance_criteria=["A", "B"],
    )
    # Pre-link a task.
    board.link_task(goal.id, "existing-task-id")
    updated = board.update(goal.id, status="in_progress")
    assert updated is not None
    # Should still have only the pre-linked task — no decomposition.
    assert list(updated.tasks) == ["existing-task-id"]


# ---------------------------------------------------------------------------
# Goal progress auto-sync on task completion
# ---------------------------------------------------------------------------



def test_goal_progress_syncs_via_task_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """task_update to 'completed' should auto-sync goal progress."""
    import asyncio

    from obscura.kairos.goals import GoalBoard
    from obscura.tools.task_tools import task_update

    db_file = tmp_path / "tasks.db"
    monkeypatch.setattr("obscura.core.task_queue._db_path", lambda: db_file)

    goals_dir = tmp_path / "goals"
    board = GoalBoard(goals_dir=goals_dir)

    goal = board.create("Progress test", priority="medium")
    q = TaskQueue()
    task_ids: list[str] = []
    for i in range(2):
        tid = q.enqueue(f"Task {i}", goal_id=goal.id)
        board.link_task(goal.id, tid)
        task_ids.append(tid)

    # Complete first task — goal should be at ~50%.
    asyncio.run(task_update(task_id=task_ids[0], status="completed"))
    goal_mid = board.load(goal.id)
    assert goal_mid is not None
    assert goal_mid.progress == 50

    # Complete second task — goal should reach 100%.
    asyncio.run(task_update(task_id=task_ids[1], status="completed"))
    goal_done = board.load(goal.id)
    assert goal_done is not None
    assert goal_done.progress == 100


def test_goal_progress_syncs_via_queue_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """queue_complete should auto-sync goal progress and set last_worked."""
    import asyncio

    from obscura.kairos.goals import GoalBoard
    from obscura.tools.task_tools import queue_complete

    db_file = tmp_path / "tasks.db"
    monkeypatch.setattr("obscura.core.task_queue._db_path", lambda: db_file)

    goals_dir = tmp_path / "goals"
    board = GoalBoard(goals_dir=goals_dir)

    goal = board.create("Queue complete test", priority="high")
    q = TaskQueue()
    task_ids: list[str] = []
    for i in range(2):
        tid = q.enqueue(f"QC Task {i}", goal_id=goal.id)
        board.link_task(goal.id, tid)
        task_ids.append(tid)

    # Complete first task via queue_complete — expect 50%.
    asyncio.run(queue_complete(task_id=task_ids[0], output="done"))
    goal_mid = board.load(goal.id)
    assert goal_mid is not None
    assert goal_mid.progress == 50
    assert goal_mid.last_worked is not None  # last_worked was set

    # Complete second task — expect 100%.
    asyncio.run(queue_complete(task_id=task_ids[1], output="done"))
    goal_done = board.load(goal.id)
    assert goal_done is not None
    assert goal_done.progress == 100


def test_goal_sync_failure_does_not_fail_task_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broken GoalBoard must not prevent task_update from succeeding."""
    import asyncio

    from obscura.tools.task_tools import task_update

    db_file = tmp_path / "tasks.db"
    monkeypatch.setattr("obscura.core.task_queue._db_path", lambda: db_file)

    q = TaskQueue()
    tid = q.enqueue("Orphan task", goal_id="nonexistent-goal")

    # Force GoalBoard.sync_task_progress to raise.
    def _bad_sync(*_a: object, **_kw: object) -> None:
        raise RuntimeError("storage failure")

    monkeypatch.setattr("obscura.kairos.goals.GoalBoard.sync_task_progress", _bad_sync)

    result_raw = asyncio.run(task_update(task_id=tid, status="completed"))
    import json as _json
    result = _json.loads(result_raw)
    assert result["ok"] is True
    assert "status" in result["updated_fields"]
    # Task itself must be completed.
    task = q.get(tid)
    assert task is not None
    assert task["status"] == "completed"

