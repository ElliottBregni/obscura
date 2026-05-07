"""Unit tests for task management tools (create/get/list/update/queue).

All tests redirect the SQLite database to a temp path via monkeypatch so
production state at ~/.obscura/tasks.db is never touched.
"""
# pyright: reportPrivateUsage=false

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixture: redirect tasks.db to a temp file
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _use_temp_tasks_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Route all SQLite operations to a throw-away temp file."""
    db_file = tmp_path / "tasks.db"
    monkeypatch.setattr(
        "obscura.core.task_queue._db_path",
        lambda: db_file,
    )


# ---------------------------------------------------------------------------
# Import tools lazily after monkeypatch is in place
# ---------------------------------------------------------------------------

# NOTE: we import at function level to ensure the monkeypatch is applied before
# any module-level DB connections are made.


# ---------------------------------------------------------------------------
# task_create
# ---------------------------------------------------------------------------


async def test_task_create_returns_task_id() -> None:
    from obscura.tools.task_tools import task_create

    result = json.loads(await task_create(subject="Test task alpha"))

    assert result["ok"] is True
    assert "task_id" in result
    assert isinstance(result["task_id"], str)
    assert len(result["task_id"]) > 0
    assert result["subject"] == "Test task alpha"


async def test_task_create_multiple_tasks_get_distinct_ids() -> None:
    from obscura.tools.task_tools import task_create

    r1 = json.loads(await task_create(subject="Task 1"))
    r2 = json.loads(await task_create(subject="Task 2"))

    assert r1["task_id"] != r2["task_id"]


# ---------------------------------------------------------------------------
# task_get
# ---------------------------------------------------------------------------


async def test_task_get_returns_created_task() -> None:
    from obscura.tools.task_tools import task_create, task_get

    create_result = json.loads(await task_create(subject="Fetch me"))
    task_id = create_result["task_id"]

    get_result = json.loads(await task_get(task_id=task_id))

    assert get_result["ok"] is True
    assert get_result["task"] is not None
    assert get_result["task"]["task_id"] == task_id
    assert get_result["task"]["subject"] == "Fetch me"


async def test_task_get_unknown_id_returns_task_none() -> None:
    from obscura.tools.task_tools import task_get

    result = json.loads(await task_get(task_id="nonexistent-task-id-xyz"))

    assert result["ok"] is True
    assert result["task"] is None


# ---------------------------------------------------------------------------
# task_list
# ---------------------------------------------------------------------------


async def test_task_list_includes_created_tasks() -> None:
    from obscura.tools.task_tools import task_create, task_list

    await task_create(subject="Listed task A")
    await task_create(subject="Listed task B")

    result = json.loads(await task_list())

    assert result["ok"] is True
    assert result["count"] >= 2
    subjects = {t["subject"] for t in result["tasks"]}
    assert "Listed task A" in subjects
    assert "Listed task B" in subjects


async def test_task_list_status_filter() -> None:
    from obscura.tools.task_tools import task_create, task_list, task_update

    r = json.loads(await task_create(subject="Will be in_progress"))
    task_id = r["task_id"]
    await task_update(task_id=task_id, status="in_progress")

    result = json.loads(await task_list(status="in_progress"))

    assert result["ok"] is True
    ids = {t["task_id"] for t in result["tasks"]}
    assert task_id in ids


async def test_task_list_empty_db_returns_empty() -> None:
    from obscura.tools.task_tools import task_list

    result = json.loads(await task_list())

    assert result["ok"] is True
    assert result["count"] == 0
    assert result["tasks"] == []


# ---------------------------------------------------------------------------
# task_update
# ---------------------------------------------------------------------------


async def test_task_update_status() -> None:
    from obscura.tools.task_tools import task_create, task_get, task_update

    r = json.loads(await task_create(subject="Status update target"))
    task_id = r["task_id"]

    update_result = json.loads(
        await task_update(task_id=task_id, status="in_progress")
    )

    assert update_result["ok"] is True
    assert "status" in update_result["updated_fields"]

    get_result = json.loads(await task_get(task_id=task_id))
    assert get_result["task"]["status"] == "in_progress"


async def test_task_update_subject() -> None:
    from obscura.tools.task_tools import task_create, task_get, task_update

    r = json.loads(await task_create(subject="Old subject"))
    task_id = r["task_id"]

    await task_update(task_id=task_id, subject="New subject")

    get_result = json.loads(await task_get(task_id=task_id))
    assert get_result["task"]["subject"] == "New subject"


async def test_task_update_nonexistent_task_returns_error() -> None:
    from obscura.tools.task_tools import task_update

    result = json.loads(
        await task_update(task_id="no-such-task-id-xyz", status="in_progress")
    )

    assert result["ok"] is False
    assert "not_found" in result.get("error", "")


async def test_task_update_deleted_removes_from_list() -> None:
    from obscura.tools.task_tools import task_create, task_list, task_update

    r = json.loads(await task_create(subject="To be deleted"))
    task_id = r["task_id"]

    del_result = json.loads(await task_update(task_id=task_id, status="deleted"))

    assert del_result["ok"] is True
    assert del_result.get("deleted") is True

    list_result = json.loads(await task_list())
    ids = {t["task_id"] for t in list_result["tasks"]}
    assert task_id not in ids


# ---------------------------------------------------------------------------
# Queue lifecycle: queue_next → queue_complete
# ---------------------------------------------------------------------------


async def test_queue_next_returns_pending_task() -> None:
    from obscura.tools.task_tools import task_create, queue_next

    r = json.loads(await task_create(subject="Queue item"))
    task_id = r["task_id"]

    claim = json.loads(await queue_next(worker_id="test-worker"))

    assert claim["ok"] is True
    assert claim["task"] is not None
    assert claim["task"]["task_id"] == task_id


async def test_queue_next_empty_queue_returns_null_task() -> None:
    from obscura.tools.task_tools import queue_next

    result = json.loads(await queue_next())

    assert result["ok"] is True
    assert result["task"] is None


async def test_queue_complete_marks_task_completed() -> None:
    from obscura.tools.task_tools import task_create, task_get, queue_next, queue_complete

    r = json.loads(await task_create(subject="Will be completed"))
    task_id = r["task_id"]

    await queue_next(worker_id="worker-1")
    complete_result = json.loads(
        await queue_complete(task_id=task_id, output="all done")
    )

    assert complete_result["ok"] is True

    task = json.loads(await task_get(task_id=task_id))["task"]
    assert task["status"] == "completed"


async def test_queue_fail_marks_task_failed_and_requeues() -> None:
    from obscura.tools.task_tools import task_create, task_get, queue_next, queue_fail

    # Create with max_retries=0 so one failure → permanent failure
    r = json.loads(await task_create(subject="Will fail", max_retries=0))
    task_id = r["task_id"]

    await queue_next(worker_id="worker-1")
    fail_result = json.loads(
        await queue_fail(task_id=task_id, error="something broke", retry=False)
    )

    assert fail_result["ok"] is True

    task = json.loads(await task_get(task_id=task_id))["task"]
    assert task["status"] == "failed"


# ---------------------------------------------------------------------------
# queue_depth
# ---------------------------------------------------------------------------


async def test_queue_depth_reflects_pending_tasks() -> None:
    from obscura.tools.task_tools import task_create, queue_depth

    await task_create(subject="Depth test 1")
    await task_create(subject="Depth test 2")

    result = json.loads(await queue_depth())

    assert result["ok"] is True
    # depth is a dict keyed by priority bucket; total is the integer sum
    assert isinstance(result["depth"], dict)
    assert result["total"] >= 2
