"""Unit tests for obscura.arbiter.watchdog — proactive health monitoring."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from obscura.arbiter.watchdog import ArbiterWatchdog


@pytest.fixture()
def watchdog() -> ArbiterWatchdog:
    return ArbiterWatchdog(
        zombie_timeout=0.01,  # Very short for testing.
        max_same_error=2,
        score_decay_window=3,
        score_decay_threshold=0.1,
    )


@pytest.fixture()
def _db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect task queue to a temp DB."""
    db_file = tmp_path / "tasks.db"
    monkeypatch.setattr("obscura.core.task_queue._db_path", lambda: db_file)


# ---------------------------------------------------------------------------
# Zombie tasks
# ---------------------------------------------------------------------------


def test_zombie_detection(watchdog: ArbiterWatchdog, _db: None) -> None:
    from obscura.core.task_queue import TaskQueue

    q = TaskQueue()
    tid = q.enqueue("Zombie task")
    q.claim(tid, "dead-worker")
    time.sleep(0.02)  # Wait for zombie timeout.

    actions = watchdog._check_zombie_tasks()
    assert len(actions) == 1
    assert actions[0].action == "release_claim"
    assert "Zombie" in actions[0].reason


def test_no_zombies_when_fresh(watchdog: ArbiterWatchdog, _db: None) -> None:
    from obscura.core.task_queue import TaskQueue

    q = TaskQueue(claim_timeout=9999)
    tid = q.enqueue("Fresh task")
    q.claim(tid, "active-worker")

    wd = ArbiterWatchdog(zombie_timeout=9999)
    actions = wd._check_zombie_tasks()
    assert len(actions) == 0


# ---------------------------------------------------------------------------
# Spinning tasks
# ---------------------------------------------------------------------------


def test_spinning_detection(watchdog: ArbiterWatchdog, _db: None) -> None:
    from obscura.core.task_queue import TaskQueue

    q = TaskQueue()
    tid = q.enqueue("Spinner", max_retries=5)
    q.claim(tid, "w")
    q.fail(tid, "same error")
    # Manually bump retry_count to trigger spinning detection.
    from obscura.core.task_queue import _open

    conn = _open()
    conn.execute(
        "UPDATE tasks SET retry_count = 3, run_after = 0 WHERE task_id = ?", (tid,)
    )
    conn.commit()
    conn.close()

    actions = watchdog._check_spinning_tasks()
    assert len(actions) == 1
    assert actions[0].action == "kill_task"
    assert "Spinning" in actions[0].reason


# ---------------------------------------------------------------------------
# Orphan tasks
# ---------------------------------------------------------------------------


def test_orphan_detection(
    watchdog: ArbiterWatchdog,
    _db: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from obscura.core.task_queue import TaskQueue
    from obscura.kairos.goals import GoalBoard

    goals_dir = tmp_path / "goals"
    monkeypatch.setattr("obscura.kairos.goals._GOALS_DIR", goals_dir)

    board = GoalBoard(goals_dir=goals_dir)
    goal = board.create("Doomed goal")
    board.abandon(goal.id, "changed plans")

    q = TaskQueue()
    tid = q.enqueue("Orphan task", goal_id=goal.id)

    actions = watchdog._check_orphan_tasks()
    assert len(actions) == 1
    assert actions[0].action == "kill_task"
    assert "Orphan" in actions[0].reason


# ---------------------------------------------------------------------------
# Score decay
# ---------------------------------------------------------------------------


def test_score_decay_detected(watchdog: ArbiterWatchdog) -> None:
    # Feed declining scores.
    watchdog.record_turn_score(0.9)
    watchdog.record_turn_score(0.7)
    watchdog.record_turn_score(0.4)

    actions = watchdog._check_score_decay()
    assert len(actions) == 1
    assert actions[0].action == "alert"
    assert "decay" in actions[0].reason.lower()


def test_score_decay_not_triggered_on_stable(watchdog: ArbiterWatchdog) -> None:
    watchdog.record_turn_score(0.8)
    watchdog.record_turn_score(0.85)
    watchdog.record_turn_score(0.82)

    actions = watchdog._check_score_decay()
    assert len(actions) == 0


# ---------------------------------------------------------------------------
# Execute actions
# ---------------------------------------------------------------------------


def test_execute_release(watchdog: ArbiterWatchdog, _db: None) -> None:
    from obscura.arbiter.watchdog import WatchdogAction
    from obscura.core.task_queue import TaskQueue

    q = TaskQueue()
    tid = q.enqueue("Release me")
    q.claim(tid, "worker-1")

    action = WatchdogAction(
        action="release_claim",
        target_id=tid,
        reason="test",
        metadata={"claimed_by": "worker-1"},
    )
    results = watchdog.execute([action])
    assert len(results) == 1
    assert "Released" in results[0]

    # Task should be unclaimed now.
    task = q.get(tid)
    assert task is not None
    assert task["claimed_by"] == ""


def test_execute_kill(watchdog: ArbiterWatchdog, _db: None) -> None:
    from obscura.arbiter.watchdog import WatchdogAction
    from obscura.core.task_queue import TaskQueue

    q = TaskQueue()
    tid = q.enqueue("Kill me")

    action = WatchdogAction(action="kill_task", target_id=tid, reason="test kill")
    results = watchdog.execute([action])
    assert len(results) == 1
    assert "Killed" in results[0]

    task = q.get(tid)
    assert task is not None
    assert task["status"] == "failed"
    assert "watchdog" in task["error"].lower()
