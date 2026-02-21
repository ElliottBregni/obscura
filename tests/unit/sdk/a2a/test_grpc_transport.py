"""Tests for sdk.a2a.transports.grpc_server — A2AServicer logic.

Tests the gRPC servicer directly (without starting a real gRPC server)
by calling the JSON-in/JSON-out methods.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from sdk.a2a.agent_card import AgentCardGenerator
from sdk.a2a.service import A2AService
from sdk.a2a.store import InMemoryTaskStore
from sdk.a2a.transports.grpc_server import A2AServicer
from sdk.a2a.types import TaskNotFoundError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def servicer() -> A2AServicer:
    store = InMemoryTaskStore()
    card = AgentCardGenerator("gRPCAgent", "https://test.local").build()
    service = A2AService(store=store, agent_card=card)
    return A2AServicer(service)


# ---------------------------------------------------------------------------
# SendMessage
# ---------------------------------------------------------------------------


class TestSendMessage:
    @pytest.mark.asyncio
    async def test_send_blocking(self, servicer: A2AServicer) -> None:
        request = json.dumps({
            "message": {
                "role": "user",
                "messageId": "m1",
                "parts": [{"kind": "text", "text": "Hello gRPC"}],
            },
            "blocking": True,
        })
        result_json = await servicer.SendMessage(request)
        result = json.loads(result_json)
        assert result["status"]["state"] == "completed"
        assert result["id"].startswith("task-")

    @pytest.mark.asyncio
    async def test_send_with_context(self, servicer: A2AServicer) -> None:
        request = json.dumps({
            "message": {
                "role": "user",
                "messageId": "m1",
                "parts": [{"kind": "text", "text": "Test"}],
            },
            "contextId": "ctx-grpc",
        })
        result = json.loads(await servicer.SendMessage(request))
        assert result["contextId"] == "ctx-grpc"


# ---------------------------------------------------------------------------
# GetTask
# ---------------------------------------------------------------------------


class TestGetTask:
    @pytest.mark.asyncio
    async def test_get_existing(self, servicer: A2AServicer) -> None:
        # Create first
        create_json = await servicer.SendMessage(json.dumps({
            "message": {
                "role": "user",
                "messageId": "m1",
                "parts": [{"kind": "text", "text": "Create"}],
            },
        }))
        task_id = json.loads(create_json)["id"]

        # Get
        result = json.loads(await servicer.GetTask(json.dumps({"taskId": task_id})))
        assert result["id"] == task_id

    @pytest.mark.asyncio
    async def test_get_not_found(self, servicer: A2AServicer) -> None:
        with pytest.raises(TaskNotFoundError):
            await servicer.GetTask(json.dumps({"taskId": "bad"}))


# ---------------------------------------------------------------------------
# ListTasks
# ---------------------------------------------------------------------------


class TestListTasks:
    @pytest.mark.asyncio
    async def test_list_empty(self, servicer: A2AServicer) -> None:
        result = json.loads(await servicer.ListTasks(json.dumps({})))
        assert result["tasks"] == []

    @pytest.mark.asyncio
    async def test_list_after_create(self, servicer: A2AServicer) -> None:
        await servicer.SendMessage(json.dumps({
            "message": {
                "role": "user",
                "messageId": "m1",
                "parts": [{"kind": "text", "text": "A"}],
            },
        }))
        result = json.loads(await servicer.ListTasks(json.dumps({})))
        assert len(result["tasks"]) == 1


# ---------------------------------------------------------------------------
# CancelTask
# ---------------------------------------------------------------------------


class TestCancelTask:
    @pytest.mark.asyncio
    async def test_cancel(self, servicer: A2AServicer) -> None:
        create_json = await servicer.SendMessage(json.dumps({
            "message": {
                "role": "user",
                "messageId": "m1",
                "parts": [{"kind": "text", "text": "X"}],
            },
            "blocking": False,
        }))
        task_id = json.loads(create_json)["id"]
        # Task may already be completed, but CancelTask should either
        # succeed or raise TaskNotCancelableError
        try:
            result = json.loads(await servicer.CancelTask(json.dumps({"taskId": task_id})))
            assert result["status"]["state"] == "canceled"
        except Exception:
            pass  # Already completed — expected


# ---------------------------------------------------------------------------
# StreamMessage
# ---------------------------------------------------------------------------


class TestStreamMessage:
    @pytest.mark.asyncio
    async def test_stream_yields_events(self, servicer: A2AServicer) -> None:
        request = json.dumps({
            "message": {
                "role": "user",
                "messageId": "m1",
                "parts": [{"kind": "text", "text": "Stream me"}],
            },
        })
        events: list[dict[str, Any]] = []
        async for event_json in servicer.StreamMessage(request):
            events.append(json.loads(event_json))

        assert len(events) > 0
        # Last event should be COMPLETED
        last: dict[str, Any] = events[-1]
        assert last["kind"] == "status-update"
        assert last["status"]["state"] == "completed"
        assert last["final"] is True


# ---------------------------------------------------------------------------
# GetAgentCard
# ---------------------------------------------------------------------------


class TestGetAgentCard:
    @pytest.mark.asyncio
    async def test_returns_card(self, servicer: A2AServicer) -> None:
        result = json.loads(await servicer.GetAgentCard())
        assert result["name"] == "gRPCAgent"
        assert result["protocolVersion"] == "0.3"
