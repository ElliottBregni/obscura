"""obscura.core.background_tasks — Background task process manager.

Allows tools (e.g. ``run_shell``) to launch long-running shell commands
in the background, retrieve their output later, and stop them on demand.

Usage::

    mgr = BackgroundTaskManager()
    task_id = await mgr.start("pytest tests/ -v", cwd="/project")
    # ... later ...
    task = mgr.get(task_id)
    assert task.status in ("running", "completed", "failed")
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import time
from datetime import UTC, datetime

from obscura.auth.secrets import safe_subprocess_env
from obscura.core.enums.lifecycle import BackgroundTaskStatus
from obscura.core.models.lifecycle import BackgroundTaskRecord

logger = logging.getLogger(__name__)


# The runtime in-memory record. Re-exported under the historical name so
# callers using ``BackgroundTask`` keep resolving without changes.
BackgroundTask = BackgroundTaskRecord


def _now_dt() -> datetime:
    return datetime.now(UTC)


class BackgroundTaskManager:
    """Manages background shell processes.

    All state is in-memory; tasks do not survive process restarts.
    """

    def __init__(self) -> None:
        self._tasks: dict[str, BackgroundTaskRecord] = {}
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._watchers: dict[str, asyncio.Task[None]] = {}

    async def start(
        self,
        command: str,
        *,
        cwd: str = "",
        timeout: float = 600.0,
    ) -> str:
        """Launch *command* in the background, returning a task ID."""
        task_id = hashlib.sha256(
            f"{command}:{cwd}:{time.time()}".encode(),
        ).hexdigest()[:12]

        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=cwd or None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=safe_subprocess_env(),
        )

        task = BackgroundTaskRecord(
            id=task_id,
            status=BackgroundTaskStatus.RUNNING,
            status_changed_at=_now_dt(),
            command=command,
            cwd=cwd,
            started_at=time.time(),
        )
        self._tasks[task_id] = task
        self._processes[task_id] = proc

        # Spawn a watcher coroutine that collects output on completion.
        self._watchers[task_id] = asyncio.create_task(
            self._watch(task_id, proc, timeout),
        )

        return task_id

    async def _watch(
        self,
        task_id: str,
        proc: asyncio.subprocess.Process,
        timeout: float,
    ) -> None:
        """Wait for the process to complete and record results."""
        task = self._tasks[task_id]
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout,
            )
            task.stdout = (
                stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else ""
            )
            task.stderr = (
                stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes else ""
            )
            task.exit_code = proc.returncode
            task.status = (
                BackgroundTaskStatus.COMPLETED
                if proc.returncode == 0
                else BackgroundTaskStatus.FAILED
            )
        except TimeoutError:
            logger.debug("suppressed exception in _watch", exc_info=True)
            proc.kill()
            await proc.wait()
            task.status = BackgroundTaskStatus.FAILED
            task.stderr = f"Timed out after {timeout}s"
            task.exit_code = -1
        except asyncio.CancelledError:
            logger.debug("suppressed exception in _watch", exc_info=True)
            proc.kill()
            await proc.wait()
            task.status = BackgroundTaskStatus.STOPPED
            task.exit_code = -1
        finally:
            task.completed_at = time.time()
            task.status_changed_at = _now_dt()
            self._processes.pop(task_id, None)
            self._watchers.pop(task_id, None)

    def get(self, task_id: str) -> BackgroundTaskRecord | None:
        """Retrieve a task by ID, or ``None`` if not found."""
        return self._tasks.get(task_id)

    async def stop(self, task_id: str) -> bool:
        """Stop a running background task. Returns ``True`` if stopped."""
        proc = self._processes.get(task_id)
        if proc is None:
            return False
        watcher = self._watchers.get(task_id)
        if watcher is not None:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher
        task = self._tasks.get(task_id)
        if task is not None:
            task.status = BackgroundTaskStatus.STOPPED
            task.status_changed_at = _now_dt()
            task.completed_at = time.time()
        return True

    def list_tasks(self) -> list[BackgroundTaskRecord]:
        """Return all tracked tasks (running and completed)."""
        return list(self._tasks.values())

    async def shutdown(self) -> None:
        """Stop all running tasks. Call during process shutdown."""
        for task_id in list(self._processes.keys()):
            await self.stop(task_id)


# Module-level singleton.
_manager: BackgroundTaskManager | None = None


def get_background_task_manager() -> BackgroundTaskManager:
    """Return the global ``BackgroundTaskManager`` singleton."""
    global _manager
    if _manager is None:
        _manager = BackgroundTaskManager()
    return _manager
