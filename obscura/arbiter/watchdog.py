"""obscura.arbiter.watchdog — Proactive task and agent health monitoring.

Unlike the reactive evaluation in ``engine.py`` (which scores actions
as they happen), the watchdog runs periodically and looks for problems
that nobody reported:

- **Zombie tasks**: Claimed but no heartbeat for too long.
- **Spinning tasks**: Failed N times with the same error.
- **Orphan tasks**: Parent goal was abandoned.
- **Score decay**: Agent quality declining over the session.
- **Drift**: Agent working on something unrelated to its assigned task.

The watchdog is called from the KAIROS tick loop (one check per tick,
lightweight, <50ms).
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

from obscura.arbiter.checks import (  # noqa: PLC2701
    _STOP_WORDS,  # pyright: ignore[reportPrivateUsage]
    _stem,  # pyright: ignore[reportPrivateUsage]
)
from obscura.core.task_queue import (
    TaskQueue,
    _open,  # pyright: ignore[reportPrivateUsage]  # noqa: PLC2701
)
from obscura.kairos.goals import GoalBoard

logger = logging.getLogger(__name__)


def _empty_metadata() -> dict[str, Any]:
    return {}


@dataclass(frozen=True)
class WatchdogAction:
    """An action the watchdog recommends."""

    action: str  # "kill_task", "release_claim", "deprioritize", "alert"
    target_id: str
    reason: str
    metadata: dict[str, Any] = field(default_factory=_empty_metadata)


class ArbiterWatchdog:
    """Proactive health monitor. Call ``sweep()`` periodically."""

    def __init__(
        self,
        *,
        zombie_timeout: float = 300.0,  # 5 min with no heartbeat
        max_same_error: int = 3,
        score_decay_window: int = 5,  # look at last N turns
        score_decay_threshold: float = 0.15,  # per-turn drop
    ) -> None:
        self._zombie_timeout = zombie_timeout
        self._max_same_error = max_same_error
        self._score_decay_window = score_decay_window
        self._score_decay_threshold = score_decay_threshold
        self._turn_scores: list[float] = []

    def record_turn_score(self, score: float) -> None:
        """Feed in each turn's composite score for decay tracking."""
        self._turn_scores.append(score)

    def sweep(self) -> list[WatchdogAction]:
        """Run all proactive checks. Returns recommended actions."""
        actions: list[WatchdogAction] = []
        actions.extend(self._check_zombie_tasks())
        actions.extend(self._check_spinning_tasks())
        actions.extend(self._check_orphan_tasks())
        actions.extend(self._check_score_decay())
        actions.extend(self._check_duplicate_work())
        actions.extend(self._check_critical_path())
        return actions

    # ------------------------------------------------------------------
    # Zombie tasks: claimed but no heartbeat
    # ------------------------------------------------------------------

    def _check_zombie_tasks(self) -> list[WatchdogAction]:
        actions: list[WatchdogAction] = []
        try:
            now = time.time()
            # Find all claimed pending tasks.
            # We check directly since list_claimed requires a worker_id.
            conn = _open()
            try:
                rows = conn.execute(
                    """SELECT task_id, subject, claimed_by, claimed_at, last_heartbeat
                       FROM tasks
                       WHERE status = 'pending'
                         AND claimed_by != ''
                         AND claimed_at < ?""",
                    (now - self._zombie_timeout,),
                ).fetchall()
                for row in rows:
                    last_beat = row["last_heartbeat"] or row["claimed_at"]
                    if now - last_beat > self._zombie_timeout:
                        actions.append(
                            WatchdogAction(
                                action="release_claim",
                                target_id=row["task_id"],
                                reason=(
                                    f"Zombie: claimed by {row['claimed_by']} "
                                    f"{int(now - row['claimed_at'])}s ago, "
                                    f"no heartbeat for {int(now - last_beat)}s"
                                ),
                                metadata={"claimed_by": row["claimed_by"]},
                            )
                        )
            finally:
                conn.close()
        except Exception:
            logger.debug("Zombie task check failed", exc_info=True)
        return actions

    # ------------------------------------------------------------------
    # Spinning tasks: same error repeated
    # ------------------------------------------------------------------

    def _check_spinning_tasks(self) -> list[WatchdogAction]:
        actions: list[WatchdogAction] = []
        try:
            conn = _open()
            try:
                rows = conn.execute(
                    """SELECT task_id, subject, error, retry_count, max_retries
                       FROM tasks
                       WHERE status = 'pending'
                         AND retry_count >= ?
                         AND error != ''""",
                    (self._max_same_error,),
                ).fetchall()
                for row in rows:
                    actions.append(
                        WatchdogAction(
                            action="kill_task",
                            target_id=row["task_id"],
                            reason=(
                                f"Spinning: {row['retry_count']} retries "
                                f"with error: {row['error'][:80]}"
                            ),
                        )
                    )
            finally:
                conn.close()
        except Exception:
            logger.debug("Spinning task check failed", exc_info=True)
        return actions

    # ------------------------------------------------------------------
    # Orphan tasks: parent goal abandoned
    # ------------------------------------------------------------------

    def _check_orphan_tasks(self) -> list[WatchdogAction]:
        actions: list[WatchdogAction] = []
        try:
            board = GoalBoard()
            conn = _open()
            try:
                rows = conn.execute(
                    """SELECT task_id, subject, goal_id
                       FROM tasks
                       WHERE status = 'pending'
                         AND goal_id != ''""",
                ).fetchall()
                for row in rows:
                    goal = board.load(row["goal_id"])
                    if goal is not None and goal.status == "abandoned":
                        actions.append(
                            WatchdogAction(
                                action="kill_task",
                                target_id=row["task_id"],
                                reason=(
                                    f"Orphan: parent goal '{row['goal_id']}' "
                                    f"was abandoned"
                                ),
                            )
                        )
            finally:
                conn.close()
        except Exception:
            logger.debug("Orphan task check failed", exc_info=True)
        return actions

    # ------------------------------------------------------------------
    # Score decay: quality dropping over turns
    # ------------------------------------------------------------------

    def _check_score_decay(self) -> list[WatchdogAction]:
        actions: list[WatchdogAction] = []
        window = self._turn_scores[-self._score_decay_window :]
        if len(window) < self._score_decay_window:
            return actions

        # Check for monotonic decline.
        declines = sum(1 for i in range(1, len(window)) if window[i] < window[i - 1])
        if declines >= len(window) - 1:
            total_drop = window[0] - window[-1]
            if total_drop >= self._score_decay_threshold * len(window):
                actions.append(
                    WatchdogAction(
                        action="alert",
                        target_id="session",
                        reason=(
                            f"Score decay: {window[0]:.2f} → {window[-1]:.2f} "
                            f"over last {len(window)} turns "
                            f"(total drop: {total_drop:.2f})"
                        ),
                        metadata={"scores": window},
                    )
                )
        return actions

    # ------------------------------------------------------------------
    # Duplicate work: same goal, near-identical subjects
    # ------------------------------------------------------------------

    def _check_duplicate_work(self) -> list[WatchdogAction]:
        """Detect pending tasks within the same goal that do the same work.

        Uses keyword overlap between task subjects to find near-duplicates.
        When two tasks share a goal and have >60% keyword overlap, the
        lower-priority task (higher priority number) is flagged for removal.
        """
        actions: list[WatchdogAction] = []
        try:

            def _kw(text: str) -> set[str]:
                words = re.findall(r"[a-zA-Z_][a-zA-Z0-9_]{2,}", text.lower())
                return {_stem(w) for w in words if w not in _STOP_WORDS}

            conn = _open()
            try:
                rows = conn.execute(
                    """SELECT task_id, subject, goal_id, priority
                       FROM tasks
                       WHERE status = 'pending'
                         AND goal_id != ''
                       ORDER BY goal_id, priority ASC"""
                ).fetchall()
            finally:
                conn.close()

            # Group by goal_id
            by_goal: dict[str, list[dict[str, Any]]] = {}
            for row in rows:
                g = str(row["goal_id"])
                row_dict: dict[str, Any] = dict(row)
                by_goal.setdefault(g, []).append(row_dict)

            killed: set[str] = set()
            for goal_id, tasks in by_goal.items():
                if len(tasks) < 2:  # noqa: PLR2004
                    continue
                for i, t1 in enumerate(tasks):
                    if t1["task_id"] in killed:
                        continue
                    kw1 = _kw(str(t1["subject"]))
                    if not kw1:
                        continue
                    for t2 in tasks[i + 1 :]:
                        if t2["task_id"] in killed:
                            continue
                        kw2 = _kw(str(t2["subject"]))
                        if not kw2:
                            continue
                        overlap = kw1 & kw2
                        ratio = len(overlap) / max(len(kw1), len(kw2))
                        if ratio >= 0.6:  # noqa: PLR2004
                            # Kill the lower-priority task (higher priority int)
                            victim = t2 if t2["priority"] >= t1["priority"] else t1
                            victim_id = str(victim["task_id"])
                            killed.add(victim_id)
                            actions.append(
                                WatchdogAction(
                                    action="kill_task",
                                    target_id=victim_id,
                                    reason=(
                                        f"Duplicate work: {ratio:.0%} keyword overlap "
                                        f"with task in same goal '{goal_id}' "
                                        f"— '{str(victim['subject'])[:60]}'"
                                    ),
                                    metadata={
                                        "goal_id": goal_id,
                                        "overlap_ratio": ratio,
                                    },
                                )
                            )
        except Exception:
            logger.debug("Duplicate work check failed", exc_info=True)
        return actions

    # ------------------------------------------------------------------
    # Critical path: completed tasks unlock dependents
    # ------------------------------------------------------------------

    def _check_critical_path(self) -> list[WatchdogAction]:
        """When a task completes, promote its dependents to high priority.

        Scans for recently completed tasks with dependents still pending
        at medium/low priority and bumps them to high (25) so they are
        picked up on the next KAIROS tick.
        """
        actions: list[WatchdogAction] = []
        try:
            conn = _open()
            try:
                # Find pending tasks blocked by a recently completed task.
                # blocked_by is stored as comma-separated task IDs.
                recent_cutoff = time.time() - 300  # completed in last 5 min
                completed = conn.execute(
                    """SELECT task_id FROM tasks
                       WHERE status = 'completed'
                         AND updated_at > ?""",
                    (recent_cutoff,),
                ).fetchall()
                completed_ids = {row["task_id"] for row in completed}

                if not completed_ids:
                    return actions

                pending = conn.execute(
                    """SELECT task_id, subject, priority, blocked_by
                       FROM tasks
                       WHERE status = 'pending'
                         AND priority > 25
                         AND blocked_by != ''"""
                ).fetchall()

                for row in pending:
                    blockers = {
                        b.strip() for b in row["blocked_by"].split(",") if b.strip()
                    }
                    # All blockers must be completed for this task to be unblocked
                    if blockers and blockers.issubset(completed_ids):
                        conn.execute(
                            "UPDATE tasks SET priority = 25, updated_at = ? WHERE task_id = ?",
                            (time.time(), row["task_id"]),
                        )
                        conn.commit()
                        actions.append(
                            WatchdogAction(
                                action="alert",
                                target_id=row["task_id"],
                                reason=(
                                    f"Critical path: promoted '{row['subject'][:60]}' "
                                    f"to high priority — all blockers completed"
                                ),
                                metadata={
                                    "old_priority": row["priority"],
                                    "new_priority": 25,
                                },
                            )
                        )
            finally:
                conn.close()
        except Exception:
            logger.debug("Critical path check failed", exc_info=True)
        return actions

    # ------------------------------------------------------------------
    # Execute recommended actions
    # ------------------------------------------------------------------

    def execute(self, actions: list[WatchdogAction]) -> list[str]:
        """Execute watchdog actions against the task queue. Returns summaries."""
        results: list[str] = []
        for action in actions:
            try:
                if action.action == "release_claim":
                    q = TaskQueue()
                    worker = action.metadata.get("claimed_by", "")
                    if worker:
                        q.release(action.target_id, worker)
                    else:
                        q.reclaim_stale()
                    results.append(f"Released: {action.target_id} — {action.reason}")

                elif action.action == "kill_task":
                    TaskQueue().fail(
                        action.target_id,
                        f"Killed by watchdog: {action.reason}",
                        retry=False,
                    )
                    results.append(f"Killed: {action.target_id} — {action.reason}")

                elif action.action == "alert":
                    results.append(f"Alert: {action.reason}")

                else:
                    results.append(f"Unknown action: {action.action}")

            except Exception as exc:
                results.append(f"Failed {action.action} on {action.target_id}: {exc}")
                logger.debug("Watchdog action failed", exc_info=True)

        return results
