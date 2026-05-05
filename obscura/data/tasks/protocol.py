"""Protocol for the task-queue repository."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from obscura.core.enums.lifecycle import TaskQueueStatus


@runtime_checkable
class TaskRepo(Protocol):
    """Backend-agnostic work-queue interface.

    Tasks live in a single ``tasks`` table with priority + claim
    semantics. Tasks transition pending → claimed → completed/failed;
    failures may be requeued with exponential back-off up to
    ``max_retries`` times.

    Implementations: :class:`obscura.data.tasks.sqlite.SqliteTaskRepo`.
    Postgres impl is a Phase 3c task.
    """

    def enqueue(
        self,
        subject: str,
        *,
        description: str = "",
        priority: int = 50,
        goal_id: str = "",
        blocked_by: list[str] | None = None,
        run_after: float = 0.0,
        max_retries: int = 3,
        metadata: dict[str, Any] | None = None,
        project_root: str = "",
    ) -> str:
        """Create a new task; returns its task_id."""
        ...

    def next_ready(
        self,
        *,
        worker_id: str = "",
        project_root: str | None = None,
    ) -> dict[str, Any] | None:
        """Return the highest-priority unclaimed, ready-to-run task."""
        ...

    def claim(self, task_id: str, worker_id: str) -> bool:
        """Atomically claim a task; returns False if already claimed."""
        ...

    def release(self, task_id: str, worker_id: str) -> bool:
        """Release a claim without completing."""
        ...

    def heartbeat(self, task_id: str, worker_id: str) -> bool:
        """Touch ``last_heartbeat`` to prove the worker is alive."""
        ...

    def complete(self, task_id: str, *, output: str = "") -> bool:
        """Mark task completed; record output."""
        ...

    def fail(self, task_id: str, error: str, *, retry: bool = True) -> bool:
        """Mark failed; optionally requeue with back-off."""
        ...

    def queue_depth(
        self,
        *,
        status: str | TaskQueueStatus = TaskQueueStatus.PENDING,
        worker_id: str = "",
        project_root: str | None = None,
    ) -> dict[str, int]:
        """Return counts by priority bucket for diagnostics."""
        ...

    def get(self, task_id: str) -> dict[str, Any] | None:
        """Fetch a single task by id."""
        ...

    def list_claimed(
        self,
        worker_id: str,
        *,
        project_root: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return all tasks currently claimed by *worker_id*."""
        ...

    def reclaim_stale(self) -> int:
        """Release all stale claims; returns count released."""
        ...
