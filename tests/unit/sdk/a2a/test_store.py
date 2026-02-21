"""Tests for sdk.a2a.store — InMemoryTaskStore."""

from __future__ import annotations

import asyncio

import pytest

from sdk.a2a.store import InMemoryTaskStore, TaskStore
from sdk.a2a.types import (
    A2AMessage,
    Artifact,
    InvalidTransitionError,
    TaskArtifactUpdateEvent,
    TaskNotCancelableError,
    TaskNotFoundError,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
    TextPart,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _msg(text: str = "hello", role: str = "user", msg_id: str = "m1") -> A2AMessage:
    return A2AMessage(role=role, messageId=msg_id, parts=[TextPart(text=text)])


def _artifact(text: str = "result", art_id: str = "art-1") -> Artifact:
    return Artifact(artifactId=art_id, parts=[TextPart(text=text)])


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


class TestProtocol:
    def test_implements_protocol(self) -> None:
        store = InMemoryTaskStore()
        assert isinstance(store, TaskStore)


# ---------------------------------------------------------------------------
# create_task
# ---------------------------------------------------------------------------


class TestCreateTask:
    @pytest.mark.asyncio
    async def test_creates_with_pending_state(self) -> None:
        store = InMemoryTaskStore()
        task = await store.create_task("ctx-1", _msg())
        assert task.status.state == TaskState.PENDING
        assert task.id.startswith("task-")
        assert task.contextId == "ctx-1"

    @pytest.mark.asyncio
    async def test_initial_message_in_history(self) -> None:
        store = InMemoryTaskStore()
        msg = _msg("Process this ticket")
        task = await store.create_task("ctx-1", msg)
        assert len(task.history) == 1
        assert task.history[0].messageId == "m1"

    @pytest.mark.asyncio
    async def test_unique_ids(self) -> None:
        store = InMemoryTaskStore()
        t1 = await store.create_task("ctx-1", _msg())
        t2 = await store.create_task("ctx-1", _msg(msg_id="m2"))
        assert t1.id != t2.id

    @pytest.mark.asyncio
    async def test_empty_artifacts(self) -> None:
        store = InMemoryTaskStore()
        task = await store.create_task("ctx-1", _msg())
        assert task.artifacts == []


# ---------------------------------------------------------------------------
# get_task
# ---------------------------------------------------------------------------


class TestGetTask:
    @pytest.mark.asyncio
    async def test_existing(self) -> None:
        store = InMemoryTaskStore()
        created = await store.create_task("ctx-1", _msg())
        found = await store.get_task(created.id)
        assert found is not None
        assert found.id == created.id

    @pytest.mark.asyncio
    async def test_missing_returns_none(self) -> None:
        store = InMemoryTaskStore()
        assert await store.get_task("nonexistent") is None


# ---------------------------------------------------------------------------
# transition (state machine)
# ---------------------------------------------------------------------------


class TestTransition:
    @pytest.mark.asyncio
    async def test_pending_to_working(self) -> None:
        store = InMemoryTaskStore()
        task = await store.create_task("ctx-1", _msg())
        updated = await store.transition(task.id, TaskState.WORKING)
        assert updated.status.state == TaskState.WORKING

    @pytest.mark.asyncio
    async def test_working_to_completed(self) -> None:
        store = InMemoryTaskStore()
        task = await store.create_task("ctx-1", _msg())
        await store.transition(task.id, TaskState.WORKING)
        updated = await store.transition(task.id, TaskState.COMPLETED)
        assert updated.status.state == TaskState.COMPLETED

    @pytest.mark.asyncio
    async def test_working_to_input_required(self) -> None:
        store = InMemoryTaskStore()
        task = await store.create_task("ctx-1", _msg())
        await store.transition(task.id, TaskState.WORKING)
        updated = await store.transition(task.id, TaskState.INPUT_REQUIRED)
        assert updated.status.state == TaskState.INPUT_REQUIRED

    @pytest.mark.asyncio
    async def test_input_required_to_working(self) -> None:
        store = InMemoryTaskStore()
        task = await store.create_task("ctx-1", _msg())
        await store.transition(task.id, TaskState.WORKING)
        await store.transition(task.id, TaskState.INPUT_REQUIRED)
        updated = await store.transition(task.id, TaskState.WORKING)
        assert updated.status.state == TaskState.WORKING

    @pytest.mark.asyncio
    async def test_invalid_transition_raises(self) -> None:
        store = InMemoryTaskStore()
        task = await store.create_task("ctx-1", _msg())
        with pytest.raises(InvalidTransitionError) as exc_info:
            await store.transition(task.id, TaskState.COMPLETED)
        assert exc_info.value.code == -32003

    @pytest.mark.asyncio
    async def test_terminal_state_blocks_further(self) -> None:
        store = InMemoryTaskStore()
        task = await store.create_task("ctx-1", _msg())
        await store.transition(task.id, TaskState.WORKING)
        await store.transition(task.id, TaskState.COMPLETED)
        with pytest.raises(InvalidTransitionError):
            await store.transition(task.id, TaskState.WORKING)

    @pytest.mark.asyncio
    async def test_transition_not_found(self) -> None:
        store = InMemoryTaskStore()
        with pytest.raises(TaskNotFoundError):
            await store.transition("bad-id", TaskState.WORKING)

    @pytest.mark.asyncio
    async def test_transition_with_message(self) -> None:
        store = InMemoryTaskStore()
        task = await store.create_task("ctx-1", _msg())
        confirm_msg = _msg("Approve tool?", role="agent", msg_id="m2")
        await store.transition(task.id, TaskState.WORKING, message=confirm_msg)
        updated = await store.get_task(task.id)
        assert updated is not None
        assert len(updated.history) == 2
        assert updated.history[-1].role == "agent"

    @pytest.mark.asyncio
    async def test_full_lifecycle(self) -> None:
        """PENDING → WORKING → INPUT_REQUIRED → WORKING → COMPLETED."""
        store = InMemoryTaskStore()
        task = await store.create_task("ctx-1", _msg())
        await store.transition(task.id, TaskState.WORKING)
        await store.transition(task.id, TaskState.INPUT_REQUIRED)
        await store.transition(task.id, TaskState.WORKING)
        final = await store.transition(task.id, TaskState.COMPLETED)
        assert final.status.state == TaskState.COMPLETED


# ---------------------------------------------------------------------------
# add_artifact
# ---------------------------------------------------------------------------


class TestAddArtifact:
    @pytest.mark.asyncio
    async def test_adds_artifact(self) -> None:
        store = InMemoryTaskStore()
        task = await store.create_task("ctx-1", _msg())
        art = _artifact()
        updated = await store.add_artifact(task.id, art)
        assert len(updated.artifacts) == 1
        assert updated.artifacts[0].artifactId == "art-1"

    @pytest.mark.asyncio
    async def test_multiple_artifacts(self) -> None:
        store = InMemoryTaskStore()
        task = await store.create_task("ctx-1", _msg())
        await store.add_artifact(task.id, _artifact("r1", "a1"))
        updated = await store.add_artifact(task.id, _artifact("r2", "a2"))
        assert len(updated.artifacts) == 2

    @pytest.mark.asyncio
    async def test_not_found(self) -> None:
        store = InMemoryTaskStore()
        with pytest.raises(TaskNotFoundError):
            await store.add_artifact("bad", _artifact())


# ---------------------------------------------------------------------------
# append_message
# ---------------------------------------------------------------------------


class TestAppendMessage:
    @pytest.mark.asyncio
    async def test_appends(self) -> None:
        store = InMemoryTaskStore()
        task = await store.create_task("ctx-1", _msg())
        reply = _msg("Confirmed", role="user", msg_id="m2")
        updated = await store.append_message(task.id, reply)
        assert len(updated.history) == 2

    @pytest.mark.asyncio
    async def test_not_found(self) -> None:
        store = InMemoryTaskStore()
        with pytest.raises(TaskNotFoundError):
            await store.append_message("bad", _msg())


# ---------------------------------------------------------------------------
# cancel_task
# ---------------------------------------------------------------------------


class TestCancelTask:
    @pytest.mark.asyncio
    async def test_cancel_pending(self) -> None:
        store = InMemoryTaskStore()
        task = await store.create_task("ctx-1", _msg())
        canceled = await store.cancel_task(task.id)
        assert canceled.status.state == TaskState.CANCELED

    @pytest.mark.asyncio
    async def test_cancel_working(self) -> None:
        store = InMemoryTaskStore()
        task = await store.create_task("ctx-1", _msg())
        await store.transition(task.id, TaskState.WORKING)
        canceled = await store.cancel_task(task.id)
        assert canceled.status.state == TaskState.CANCELED

    @pytest.mark.asyncio
    async def test_cancel_completed_raises(self) -> None:
        store = InMemoryTaskStore()
        task = await store.create_task("ctx-1", _msg())
        await store.transition(task.id, TaskState.WORKING)
        await store.transition(task.id, TaskState.COMPLETED)
        with pytest.raises(TaskNotCancelableError) as exc_info:
            await store.cancel_task(task.id)
        assert exc_info.value.code == -32002

    @pytest.mark.asyncio
    async def test_cancel_not_found(self) -> None:
        store = InMemoryTaskStore()
        with pytest.raises(TaskNotFoundError):
            await store.cancel_task("bad")


# ---------------------------------------------------------------------------
# list_tasks
# ---------------------------------------------------------------------------


class TestListTasks:
    @pytest.mark.asyncio
    async def test_list_by_context(self) -> None:
        store = InMemoryTaskStore()
        await store.create_task("ctx-1", _msg(msg_id="m1"))
        await store.create_task("ctx-1", _msg(msg_id="m2"))
        await store.create_task("ctx-2", _msg(msg_id="m3"))

        tasks, cursor = await store.list_tasks(context_id="ctx-1")
        assert len(tasks) == 2
        assert cursor is None

    @pytest.mark.asyncio
    async def test_list_all(self) -> None:
        store = InMemoryTaskStore()
        await store.create_task("ctx-1", _msg(msg_id="m1"))
        await store.create_task("ctx-2", _msg(msg_id="m2"))

        tasks, _ = await store.list_tasks()
        assert len(tasks) == 2

    @pytest.mark.asyncio
    async def test_filter_by_state(self) -> None:
        store = InMemoryTaskStore()
        t1 = await store.create_task("ctx-1", _msg(msg_id="m1"))
        await store.create_task("ctx-1", _msg(msg_id="m2"))
        await store.transition(t1.id, TaskState.WORKING)

        tasks, _ = await store.list_tasks(context_id="ctx-1", state=TaskState.WORKING)
        assert len(tasks) == 1
        assert tasks[0].id == t1.id

    @pytest.mark.asyncio
    async def test_pagination(self) -> None:
        store = InMemoryTaskStore()
        for i in range(5):
            await store.create_task("ctx-1", _msg(msg_id=f"m{i}"))

        page1, cursor = await store.list_tasks(context_id="ctx-1", limit=2)
        assert len(page1) == 2
        assert cursor is not None

        page2, cursor2 = await store.list_tasks(context_id="ctx-1", limit=2, cursor=cursor)
        assert len(page2) == 2
        assert cursor2 is not None

        page3, cursor3 = await store.list_tasks(context_id="ctx-1", limit=2, cursor=cursor2)
        assert len(page3) == 1
        assert cursor3 is None

    @pytest.mark.asyncio
    async def test_empty_context(self) -> None:
        store = InMemoryTaskStore()
        tasks, cursor = await store.list_tasks(context_id="nonexistent")
        assert tasks == []
        assert cursor is None

    @pytest.mark.asyncio
    async def test_limit_clamped(self) -> None:
        store = InMemoryTaskStore()
        await store.create_task("ctx-1", _msg())
        # limit=0 should be clamped to 1
        tasks, _ = await store.list_tasks(limit=0)
        assert len(tasks) == 1


# ---------------------------------------------------------------------------
# subscribe / publish_update
# ---------------------------------------------------------------------------


class TestPubSub:
    @pytest.mark.asyncio
    async def test_publish_and_receive(self) -> None:
        store = InMemoryTaskStore()
        task = await store.create_task("ctx-1", _msg())

        events_received: list[object] = []

        async def subscriber() -> None:
            async for event in store.subscribe(task.id):
                events_received.append(event)

        sub_task = asyncio.create_task(subscriber())

        # Give subscriber time to register
        await asyncio.sleep(0.01)

        # Publish a status update
        await store.publish_update(
            task.id,
            TaskStatusUpdateEvent(
                taskId=task.id,
                contextId="ctx-1",
                status=TaskStatus(state=TaskState.WORKING),
            ),
        )

        # Publish a final event to end the subscriber loop
        await store.publish_update(
            task.id,
            TaskStatusUpdateEvent(
                taskId=task.id,
                contextId="ctx-1",
                status=TaskStatus(state=TaskState.COMPLETED),
                final=True,
            ),
        )

        await asyncio.wait_for(sub_task, timeout=2.0)
        assert len(events_received) == 2
        assert isinstance(events_received[0], TaskStatusUpdateEvent)
        assert events_received[0].status.state == TaskState.WORKING
        assert events_received[1].final

    @pytest.mark.asyncio
    async def test_artifact_event(self) -> None:
        store = InMemoryTaskStore()
        task = await store.create_task("ctx-1", _msg())

        events_received: list[object] = []

        async def subscriber() -> None:
            async for event in store.subscribe(task.id):
                events_received.append(event)
                if isinstance(event, TaskStatusUpdateEvent) and event.final:
                    break

        sub_task = asyncio.create_task(subscriber())
        await asyncio.sleep(0.01)

        await store.publish_update(
            task.id,
            TaskArtifactUpdateEvent(
                taskId=task.id,
                contextId="ctx-1",
                artifact=_artifact("chunk 1", "art-1"),
                append=True,
                lastChunk=False,
            ),
        )

        await store.publish_update(
            task.id,
            TaskStatusUpdateEvent(
                taskId=task.id,
                contextId="ctx-1",
                status=TaskStatus(state=TaskState.COMPLETED),
                final=True,
            ),
        )

        await asyncio.wait_for(sub_task, timeout=2.0)
        assert len(events_received) == 2
        assert isinstance(events_received[0], TaskArtifactUpdateEvent)
        assert events_received[0].append is True

    @pytest.mark.asyncio
    async def test_subscribe_not_found(self) -> None:
        store = InMemoryTaskStore()
        with pytest.raises(TaskNotFoundError):
            async for _ in store.subscribe("nonexistent"):
                pass

    @pytest.mark.asyncio
    async def test_publish_no_subscribers(self) -> None:
        """publish_update is a no-op when there are no subscribers."""
        store = InMemoryTaskStore()
        task = await store.create_task("ctx-1", _msg())
        # Should not raise
        await store.publish_update(
            task.id,
            TaskStatusUpdateEvent(
                taskId=task.id,
                contextId="ctx-1",
                status=TaskStatus(state=TaskState.WORKING),
            ),
        )

    @pytest.mark.asyncio
    async def test_multiple_subscribers(self) -> None:
        store = InMemoryTaskStore()
        task = await store.create_task("ctx-1", _msg())

        results_a: list[object] = []
        results_b: list[object] = []

        async def sub(dest: list[object]) -> None:
            async for event in store.subscribe(task.id):
                dest.append(event)

        ta = asyncio.create_task(sub(results_a))
        tb = asyncio.create_task(sub(results_b))
        await asyncio.sleep(0.01)

        await store.publish_update(
            task.id,
            TaskStatusUpdateEvent(
                taskId=task.id,
                contextId="ctx-1",
                status=TaskStatus(state=TaskState.COMPLETED),
                final=True,
            ),
        )

        await asyncio.wait_for(asyncio.gather(ta, tb), timeout=2.0)
        assert len(results_a) == 1
        assert len(results_b) == 1
