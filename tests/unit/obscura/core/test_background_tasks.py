"""Tests for obscura.core.background_tasks."""

from __future__ import annotations

import asyncio

from obscura.core.background_tasks import BackgroundTaskManager


async def test_start_and_get() -> None:
    mgr = BackgroundTaskManager()
    task_id = await mgr.start("echo hello", timeout=10.0)
    assert task_id
    # Wait briefly for completion.
    await asyncio.sleep(0.5)
    task = mgr.get(task_id)
    assert task is not None
    assert task.status in ("completed", "running")
    await mgr.shutdown()


async def test_list_tasks() -> None:
    mgr = BackgroundTaskManager()
    await mgr.start("echo one", timeout=5.0)
    await mgr.start("echo two", timeout=5.0)
    tasks = mgr.list_tasks()
    assert len(tasks) == 2
    await mgr.shutdown()


async def test_stop_task() -> None:
    mgr = BackgroundTaskManager()
    task_id = await mgr.start("sleep 60", timeout=120.0)
    assert mgr.get(task_id) is not None
    stopped = await mgr.stop(task_id)
    assert stopped
    task = mgr.get(task_id)
    assert task is not None
    assert task.status == "stopped"
    await mgr.shutdown()


async def test_get_nonexistent() -> None:
    mgr = BackgroundTaskManager()
    assert mgr.get("nonexistent") is None
    await mgr.shutdown()
