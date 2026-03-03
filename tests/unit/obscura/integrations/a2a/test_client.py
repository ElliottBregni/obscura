"""Tests for sdk.a2a.client — A2AClient and A2ASessionManager.

Tests use a real FastAPI test server running the A2A transports,
giving us true end-to-end client↔server testing without mocks.
"""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from obscura.integrations.a2a.agent_card import AgentCardGenerator
from obscura.integrations.a2a.client import A2AClient, A2ASessionManager
from obscura.integrations.a2a.service import A2AService
from obscura.integrations.a2a.store import InMemoryTaskStore
from obscura.integrations.a2a.transports.jsonrpc import create_jsonrpc_router
from obscura.integrations.a2a.transports.rest import (
    create_rest_router,
    create_wellknown_router,
)
from obscura.integrations.a2a.transports.sse import create_sse_router
from obscura.integrations.a2a.types import A2AError, TaskState


# ---------------------------------------------------------------------------
# Test server fixture
# ---------------------------------------------------------------------------


def _make_app() -> FastAPI:
    store = InMemoryTaskStore()
    card = AgentCardGenerator(
        "ClientTestAgent",
        "https://test.local",
        description="Test agent for client tests",
    ).build()
    service = A2AService(store=store, agent_card=card)

    app = FastAPI()
    app.include_router(create_jsonrpc_router(service))
    app.include_router(create_rest_router(service))
    app.include_router(create_wellknown_router(service))
    app.include_router(create_sse_router(service))
    return app


@pytest.fixture
def app() -> FastAPI:
    return _make_app()


@pytest.fixture
async def client(app: FastAPI):
    """A2AClient wired to the test server."""
    transport = ASGITransport(app=app)
    c = A2AClient("http://test")
    # Inject httpx client with ASGI transport
    import httpx

    c._http = httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"A2A-Version": "0.3"},
        timeout=30.0,
    )
    yield c
    await c.disconnect()


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


class TestDiscover:
    @pytest.mark.asyncio
    async def test_discover_agent_card(self, client: A2AClient) -> None:
        card = await client.discover()
        assert card.name == "ClientTestAgent"
        assert card.protocolVersion == "0.3"
        assert client.agent_card is not None


# ---------------------------------------------------------------------------
# message/send
# ---------------------------------------------------------------------------


class TestSendMessage:
    @pytest.mark.asyncio
    async def test_send_blocking(self, client: A2AClient) -> None:
        task = await client.send_message("Hello remote agent")
        assert task.status.state == TaskState.COMPLETED
        assert task.id.startswith("task-")

    @pytest.mark.asyncio
    async def test_send_with_context(self, client: A2AClient) -> None:
        task = await client.send_message("Test", context_id="ctx-remote")
        assert task.contextId == "ctx-remote"

    @pytest.mark.asyncio
    async def test_send_non_blocking(self, client: A2AClient) -> None:
        task = await client.send_message("Quick task", blocking=False)
        assert task.status.state in (
            TaskState.PENDING,
            TaskState.WORKING,
            TaskState.COMPLETED,
        )


# ---------------------------------------------------------------------------
# tasks/get
# ---------------------------------------------------------------------------


class TestGetTask:
    @pytest.mark.asyncio
    async def test_get_existing(self, client: A2AClient) -> None:
        created = await client.send_message("Create first")
        fetched = await client.get_task(created.id)
        assert fetched.id == created.id

    @pytest.mark.asyncio
    async def test_get_not_found(self, client: A2AClient) -> None:
        with pytest.raises(A2AError) as exc_info:
            await client.get_task("nonexistent")
        assert exc_info.value.code == -32001


# ---------------------------------------------------------------------------
# tasks/list
# ---------------------------------------------------------------------------


class TestListTasks:
    @pytest.mark.asyncio
    async def test_list_empty(self, client: A2AClient) -> None:
        tasks, cursor = await client.list_tasks()
        assert tasks == []
        assert cursor is None

    @pytest.mark.asyncio
    async def test_list_after_send(self, client: A2AClient) -> None:
        await client.send_message("Task A")
        await client.send_message("Task B")
        tasks, _ = await client.list_tasks()
        assert len(tasks) == 2

    @pytest.mark.asyncio
    async def test_list_with_state_filter(self, client: A2AClient) -> None:
        await client.send_message("Done")
        tasks, _ = await client.list_tasks(state=TaskState.COMPLETED)
        assert all(t.status.state == TaskState.COMPLETED for t in tasks)


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------


class TestClientProperties:
    @pytest.mark.asyncio
    async def test_base_url(self, client: A2AClient) -> None:
        assert client.base_url == "http://test"

    @pytest.mark.asyncio
    async def test_agent_card_none_before_discover(self) -> None:
        c = A2AClient("http://nowhere")
        assert c.agent_card is None

    @pytest.mark.asyncio
    async def test_connect_disconnect(self) -> None:
        c = A2AClient("http://nowhere")
        await c.connect()
        assert c._http is not None
        await c.disconnect()
        assert c._http is None


# ---------------------------------------------------------------------------
# Context manager
# ---------------------------------------------------------------------------


class TestContextManager:
    @pytest.mark.asyncio
    async def test_async_context_manager(self) -> None:
        async with A2AClient("http://nowhere") as c:
            assert c._http is not None
        assert c._http is None


# ---------------------------------------------------------------------------
# Session Manager
# ---------------------------------------------------------------------------


class TestSessionManager:
    @pytest.mark.asyncio
    async def test_add_and_get(self) -> None:
        mgr = A2ASessionManager()
        client = await mgr.add("test", "http://nowhere")
        assert mgr.get("test") is client
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_remove(self) -> None:
        mgr = A2ASessionManager()
        await mgr.add("test", "http://nowhere")
        await mgr.remove("test")
        assert mgr.get("test") is None

    @pytest.mark.asyncio
    async def test_list_sessions(self) -> None:
        mgr = A2ASessionManager()
        await mgr.add("a", "http://a")
        await mgr.add("b", "http://b")
        names = mgr.list_sessions()
        assert set(names) == {"a", "b"}
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_close_all(self) -> None:
        mgr = A2ASessionManager()
        await mgr.add("x", "http://x")
        await mgr.add("y", "http://y")
        await mgr.close_all()
        assert mgr.list_sessions() == []
