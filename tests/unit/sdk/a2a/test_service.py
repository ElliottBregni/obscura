"""Tests for sdk.a2a.service — A2AService core business logic."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from sdk.a2a.agent_card import AgentCardGenerator
from sdk.a2a.service import A2AService
from sdk.a2a.store import InMemoryTaskStore
from sdk.a2a.types import (
    A2AMessage,
    StreamEvent,
    TaskArtifactUpdateEvent,
    TaskState,
    TaskStatusUpdateEvent,
    TextPart,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


from typing import Literal


def _msg(text: str = "hello", role: Literal["user", "agent"] = "user", msg_id: str = "m1") -> A2AMessage:
    return A2AMessage(role=role, messageId=msg_id, parts=[TextPart(text=text)])


def _card():
    return AgentCardGenerator("TestAgent", "https://test.local/a2a").build()


def _service(store: InMemoryTaskStore | None = None) -> A2AService:
    """Create a service without an agent runtime (placeholder mode)."""
    return A2AService(
        store=store or InMemoryTaskStore(),
        agent_card=_card(),
    )


# ---------------------------------------------------------------------------
# Agent Card
# ---------------------------------------------------------------------------


class TestGetAgentCard:
    def test_returns_card(self) -> None:
        svc = _service()
        card = svc.get_agent_card()
        assert card.name == "TestAgent"
        assert card.url == "https://test.local/a2a"


# ---------------------------------------------------------------------------
# message/send — blocking
# ---------------------------------------------------------------------------


class TestMessageSend:
    @pytest.mark.asyncio
    async def test_creates_task(self) -> None:
        svc = _service()
        task = await svc.message_send(_msg("Process this"), blocking=True)
        assert task.id.startswith("task-")
        assert task.status.state == TaskState.COMPLETED

    @pytest.mark.asyncio
    async def test_task_has_artifact(self) -> None:
        svc = _service()
        task = await svc.message_send(_msg("Process this"), blocking=True)
        assert len(task.artifacts) == 1
        # Placeholder text includes the prompt
        text = task.artifacts[0].parts[0].text  # type: ignore[union-attr]
        assert "Process this" in text

    @pytest.mark.asyncio
    async def test_context_id_auto_generated(self) -> None:
        svc = _service()
        task = await svc.message_send(_msg("Test"))
        assert task.contextId.startswith("ctx-")

    @pytest.mark.asyncio
    async def test_explicit_context_id(self) -> None:
        svc = _service()
        task = await svc.message_send(_msg("Test"), context_id="my-ctx")
        assert task.contextId == "my-ctx"

    @pytest.mark.asyncio
    async def test_non_blocking_returns_pending_or_working(self) -> None:
        svc = _service()
        task = await svc.message_send(_msg("Test"), blocking=False)
        assert task.status.state in (TaskState.PENDING, TaskState.WORKING)
        # Give the background task a moment to complete
        await asyncio.sleep(0.05)

    @pytest.mark.asyncio
    async def test_message_in_history(self) -> None:
        store = InMemoryTaskStore()
        svc = _service(store)
        task = await svc.message_send(_msg("Initial message"))
        stored = await store.get_task(task.id)
        assert stored is not None
        assert len(stored.history) >= 1


# ---------------------------------------------------------------------------
# message/stream
# ---------------------------------------------------------------------------


class TestMessageStream:
    @pytest.mark.asyncio
    async def test_yields_events(self) -> None:
        svc = _service()
        events: list[StreamEvent] = []
        async for event in svc.message_stream(_msg("Stream this")):
            events.append(event)

        assert len(events) > 0
        # Should end with a COMPLETED final event
        last = events[-1]
        assert isinstance(last, TaskStatusUpdateEvent)
        assert last.status.state == TaskState.COMPLETED
        assert last.final

    @pytest.mark.asyncio
    async def test_starts_with_working(self) -> None:
        svc = _service()
        events: list[StreamEvent] = []
        async for event in svc.message_stream(_msg("Stream")):
            events.append(event)
        first = events[0]
        assert isinstance(first, TaskStatusUpdateEvent)
        assert first.status.state == TaskState.WORKING

    @pytest.mark.asyncio
    async def test_has_artifact_events(self) -> None:
        svc = _service()
        events: list[StreamEvent] = []
        async for event in svc.message_stream(_msg("Content please")):
            events.append(event)
        artifact_events = [e for e in events if isinstance(e, TaskArtifactUpdateEvent)]
        assert len(artifact_events) > 0


# ---------------------------------------------------------------------------
# tasks/get
# ---------------------------------------------------------------------------


class TestTasksGet:
    @pytest.mark.asyncio
    async def test_get_existing(self) -> None:
        svc = _service()
        task = await svc.message_send(_msg("Test"))
        found = await svc.tasks_get(task.id)
        assert found is not None
        assert found.id == task.id

    @pytest.mark.asyncio
    async def test_get_missing(self) -> None:
        svc = _service()
        found = await svc.tasks_get("nonexistent")
        assert found is None


# ---------------------------------------------------------------------------
# tasks/list
# ---------------------------------------------------------------------------


class TestTasksList:
    @pytest.mark.asyncio
    async def test_list_all(self) -> None:
        svc = _service()
        await svc.message_send(_msg("Task 1"))
        await svc.message_send(_msg("Task 2", msg_id="m2"))
        tasks, _cursor = await svc.tasks_list()
        assert len(tasks) == 2

    @pytest.mark.asyncio
    async def test_list_by_context(self) -> None:
        svc = _service()
        await svc.message_send(_msg("A"), context_id="ctx-a")
        await svc.message_send(_msg("B", msg_id="m2"), context_id="ctx-b")
        tasks, _ = await svc.tasks_list(context_id="ctx-a")
        assert len(tasks) == 1

    @pytest.mark.asyncio
    async def test_list_by_state(self) -> None:
        store = InMemoryTaskStore()
        svc = _service(store)
        await svc.message_send(_msg("Done"))
        tasks, _ = await svc.tasks_list(state=TaskState.COMPLETED)
        assert len(tasks) >= 1


# ---------------------------------------------------------------------------
# tasks/cancel
# ---------------------------------------------------------------------------


class TestTasksCancel:
    @pytest.mark.asyncio
    async def test_cancel_pending(self) -> None:
        store = InMemoryTaskStore()
        svc = _service(store)
        task = await store.create_task("ctx-1", _msg())
        canceled = await svc.tasks_cancel(task.id)
        assert canceled.status.state == TaskState.CANCELED

    @pytest.mark.asyncio
    async def test_cancel_cleans_confirmations(self) -> None:
        svc = _service()
        # Simulate a pending confirmation
        evt = asyncio.Event()
        svc._pending_confirmations["task-fake"] = (evt, {"approved": False})
        # Create a real task to cancel
        task = await svc._store.create_task("ctx-1", _msg())
        svc._pending_confirmations[task.id] = (evt, {"approved": False})
        await svc.tasks_cancel(task.id)
        assert task.id not in svc._pending_confirmations


# ---------------------------------------------------------------------------
# tasks/subscribe
# ---------------------------------------------------------------------------


class TestTasksSubscribe:
    @pytest.mark.asyncio
    async def test_subscribe_receives_events(self) -> None:
        store = InMemoryTaskStore()
        svc = _service(store)
        task = await store.create_task("ctx-1", _msg())

        events_received: list[object] = []

        async def subscriber():
            async for event in svc.tasks_subscribe(task.id):
                events_received.append(event)

        sub = asyncio.create_task(subscriber())
        await asyncio.sleep(0.01)

        await store.publish_update(
            task.id,
            TaskStatusUpdateEvent(
                taskId=task.id,
                contextId="ctx-1",
                status=task.status,
                final=True,
            ),
        )

        await asyncio.wait_for(sub, timeout=2.0)
        assert len(events_received) == 1


# ---------------------------------------------------------------------------
# Text extraction helpers
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_extract_text(self) -> None:
        msg = _msg("Hello world")
        text = A2AService._extract_text(msg)
        assert text == "Hello world"

    def test_extract_text_multiple_parts(self) -> None:
        msg = A2AMessage(
            role="user",
            messageId="m1",
            parts=[TextPart(text="Part 1"), TextPart(text="Part 2")],
        )
        text = A2AService._extract_text(msg)
        assert "Part 1" in text
        assert "Part 2" in text

    def test_extract_text_empty(self) -> None:
        from sdk.a2a.types import DataPart

        msg = A2AMessage(
            role="user",
            messageId="m1",
            parts=[DataPart(data={"key": "val"})],
        )
        text = A2AService._extract_text(msg)
        assert text == "[empty message]"

    def test_extract_from_history(self) -> None:
        from sdk.a2a.types import Task, TaskStatus

        task = Task(
            id="t1",
            contextId="c1",
            status=TaskStatus(state=TaskState.PENDING),
            history=[
                _msg("First message"),
                _msg("Agent reply", role="agent", msg_id="m2"),
            ],
        )
        text = A2AService._extract_text_from_history(task)
        assert text == "First message"


# ---------------------------------------------------------------------------
# Store property
# ---------------------------------------------------------------------------


class TestProperties:
    def test_store_property(self) -> None:
        store = InMemoryTaskStore()
        svc = A2AService(store=store, agent_card=_card())
        assert svc.store is store

    def test_agent_card_property(self) -> None:
        card = _card()
        svc = A2AService(store=InMemoryTaskStore(), agent_card=card)
        assert svc.agent_card is card


class TestAPERLoopIntegration:
    @pytest.mark.asyncio
    async def test_execute_agent_uses_run_loop(self) -> None:
        fake_agent = MagicMock()
        fake_agent.start = AsyncMock()
        fake_agent.stop = AsyncMock()
        fake_agent.run_loop = AsyncMock(return_value="loop-result")

        fake_runtime = MagicMock()
        fake_runtime.spawn = MagicMock(return_value=fake_agent)

        svc = A2AService(
            store=InMemoryTaskStore(),
            agent_card=_card(),
            get_runtime=lambda: fake_runtime,
            agent_model="claude",
            agent_max_turns=7,
        )

        task = await svc.store.create_task("ctx-1", _msg("hello"))
        result = await svc._execute_agent(task, "hello")
        assert result == "loop-result"
        fake_agent.run_loop.assert_awaited_once()
        assert fake_agent.run_loop.await_args.args == ("hello",)
        run_loop_kwargs = fake_agent.run_loop.await_args.kwargs
        assert run_loop_kwargs["max_turns"] == 7
        assert callable(run_loop_kwargs["on_confirm"])

    @pytest.mark.asyncio
    async def test_execute_agent_stream_uses_stream_loop_max_turns(self) -> None:
        from sdk.internal.types import AgentEvent, AgentEventKind

        async def _event_stream():
            yield AgentEvent(
                kind=AgentEventKind.TEXT_DELTA,
                text="chunk",
            )

        fake_agent = MagicMock()
        fake_agent.start = AsyncMock()
        fake_agent.stop = AsyncMock()
        fake_agent.stream_loop = MagicMock(return_value=_event_stream())

        fake_runtime = MagicMock()
        fake_runtime.spawn = MagicMock(return_value=fake_agent)

        svc = A2AService(
            store=InMemoryTaskStore(),
            agent_card=_card(),
            get_runtime=lambda: fake_runtime,
            agent_max_turns=5,
        )

        task = await svc.store.create_task("ctx-1", _msg("hello"))
        events = [event async for event in svc._execute_agent_stream(task, "hello")]
        assert len(events) == 1
        assert events[0].kind == AgentEventKind.TEXT_DELTA
        fake_agent.stream_loop.assert_called_once()
        kwargs = fake_agent.stream_loop.call_args.kwargs
        assert kwargs["max_turns"] == 5
        assert callable(kwargs["on_confirm"])
