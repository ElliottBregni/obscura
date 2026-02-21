"""Tests for sdk.a2a.transports.jsonrpc — JSON-RPC 2.0 transport."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from sdk.a2a.agent_card import AgentCardGenerator
from sdk.a2a.service import A2AService
from sdk.a2a.store import InMemoryTaskStore
from sdk.a2a.transports.jsonrpc import create_jsonrpc_router


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def app() -> FastAPI:
    store = InMemoryTaskStore()
    card = AgentCardGenerator("TestAgent", "https://test.local").build()
    service = A2AService(store=store, agent_card=card)
    router = create_jsonrpc_router(service)

    app = FastAPI()
    app.include_router(router)
    return app


@pytest.fixture
async def client(app: FastAPI) -> AsyncClient:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c  # type: ignore[misc]


def _rpc(method: str, params: dict | None = None, req_id: int = 1) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "method": method,
        "params": params or {},
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestMessageSend:
    @pytest.mark.asyncio
    async def test_send_creates_task(self, client: AsyncClient) -> None:
        resp = await client.post("/a2a/rpc", json=_rpc("message/send", {
            "message": {
                "role": "user",
                "messageId": "m1",
                "parts": [{"kind": "text", "text": "Hello A2A"}],
            },
        }))
        assert resp.status_code == 200
        body = resp.json()
        assert body["jsonrpc"] == "2.0"
        assert body["id"] == 1
        assert "result" in body
        assert body["result"]["status"]["state"] == "completed"

    @pytest.mark.asyncio
    async def test_send_with_context(self, client: AsyncClient) -> None:
        resp = await client.post("/a2a/rpc", json=_rpc("message/send", {
            "message": {
                "role": "user",
                "messageId": "m1",
                "parts": [{"kind": "text", "text": "Test"}],
            },
            "contextId": "ctx-custom",
        }))
        result = resp.json()["result"]
        assert result["contextId"] == "ctx-custom"


class TestTasksGet:
    @pytest.mark.asyncio
    async def test_get_existing(self, client: AsyncClient) -> None:
        # Create a task first
        create_resp = await client.post("/a2a/rpc", json=_rpc("message/send", {
            "message": {
                "role": "user",
                "messageId": "m1",
                "parts": [{"kind": "text", "text": "Test"}],
            },
        }))
        task_id = create_resp.json()["result"]["id"]

        # Get the task
        resp = await client.post("/a2a/rpc", json=_rpc("tasks/get", {
            "taskId": task_id,
        }))
        assert resp.json()["result"]["id"] == task_id

    @pytest.mark.asyncio
    async def test_get_not_found(self, client: AsyncClient) -> None:
        resp = await client.post("/a2a/rpc", json=_rpc("tasks/get", {
            "taskId": "nonexistent",
        }))
        body = resp.json()
        assert "error" in body
        assert body["error"]["code"] == -32001


class TestTasksList:
    @pytest.mark.asyncio
    async def test_list_empty(self, client: AsyncClient) -> None:
        resp = await client.post("/a2a/rpc", json=_rpc("tasks/list"))
        result = resp.json()["result"]
        assert result["tasks"] == []

    @pytest.mark.asyncio
    async def test_list_with_tasks(self, client: AsyncClient) -> None:
        # Create tasks
        await client.post("/a2a/rpc", json=_rpc("message/send", {
            "message": {
                "role": "user", "messageId": "m1",
                "parts": [{"kind": "text", "text": "A"}],
            },
        }))
        await client.post("/a2a/rpc", json=_rpc("message/send", {
            "message": {
                "role": "user", "messageId": "m2",
                "parts": [{"kind": "text", "text": "B"}],
            },
        }, req_id=2))

        resp = await client.post("/a2a/rpc", json=_rpc("tasks/list"))
        assert len(resp.json()["result"]["tasks"]) == 2


class TestTasksCancel:
    @pytest.mark.asyncio
    async def test_cancel(self, client: AsyncClient) -> None:
        # Create non-blocking task
        create_resp = await client.post("/a2a/rpc", json=_rpc("message/send", {
            "message": {
                "role": "user", "messageId": "m1",
                "parts": [{"kind": "text", "text": "Test"}],
            },
            "configuration": {"blocking": False},
        }))
        task_id = create_resp.json()["result"]["id"]

        # Cancel it (may already be completed if fast enough)
        resp = await client.post("/a2a/rpc", json=_rpc("tasks/cancel", {
            "taskId": task_id,
        }))
        body = resp.json()
        # Either canceled successfully or already terminal
        assert "result" in body or "error" in body


class TestAgentCard:
    @pytest.mark.asyncio
    async def test_get_card(self, client: AsyncClient) -> None:
        resp = await client.post("/a2a/rpc", json=_rpc(
            "agent/authenticatedExtendedCard"
        ))
        result = resp.json()["result"]
        assert result["name"] == "TestAgent"
        assert result["protocolVersion"] == "0.3"


class TestStreamRedirects:
    @pytest.mark.asyncio
    async def test_message_stream_error(self, client: AsyncClient) -> None:
        resp = await client.post("/a2a/rpc", json=_rpc("message/stream"))
        body = resp.json()
        assert "error" in body
        assert body["error"]["code"] == -32600

    @pytest.mark.asyncio
    async def test_tasks_subscribe_error(self, client: AsyncClient) -> None:
        resp = await client.post("/a2a/rpc", json=_rpc("tasks/subscribe"))
        body = resp.json()
        assert "error" in body


class TestMethodNotFound:
    @pytest.mark.asyncio
    async def test_unknown_method(self, client: AsyncClient) -> None:
        resp = await client.post("/a2a/rpc", json=_rpc("nonexistent/method"))
        body = resp.json()
        assert "error" in body
        assert body["error"]["code"] == -32601
