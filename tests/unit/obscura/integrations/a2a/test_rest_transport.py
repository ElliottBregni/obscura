"""Tests for sdk.a2a.transports.rest — REST transport."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from obscura.integrations.a2a.agent_card import AgentCardGenerator
from obscura.integrations.a2a.service import A2AService
from obscura.integrations.a2a.store import InMemoryTaskStore
from obscura.integrations.a2a.transports.rest import create_rest_router, create_wellknown_router


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def app() -> FastAPI:
    store = InMemoryTaskStore()
    card = AgentCardGenerator("RestAgent", "https://test.local").build()
    service = A2AService(store=store, agent_card=card)

    app = FastAPI()
    app.include_router(create_rest_router(service))
    app.include_router(create_wellknown_router(service))
    return app


@pytest.fixture
async def client(app: FastAPI) -> AsyncGenerator[AsyncClient, None]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _task_body(text: str = "Hello", msg_id: str = "m1", blocking: bool = True) -> dict[str, Any]:
    return {
        "message": {
            "role": "user",
            "messageId": msg_id,
            "parts": [{"kind": "text", "text": text}],
        },
        "blocking": blocking,
    }


# ---------------------------------------------------------------------------
# POST /a2a/v1/tasks
# ---------------------------------------------------------------------------


class TestCreateTask:
    @pytest.mark.asyncio
    async def test_creates_task(self, client: AsyncClient) -> None:
        resp = await client.post("/a2a/v1/tasks", json=_task_body())
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"]["state"] == "completed"
        assert data["id"].startswith("task-")

    @pytest.mark.asyncio
    async def test_with_context_id(self, client: AsyncClient) -> None:
        body = _task_body()
        body["contextId"] = "my-ctx"
        resp = await client.post("/a2a/v1/tasks", json=body)
        assert resp.json()["contextId"] == "my-ctx"


# ---------------------------------------------------------------------------
# GET /a2a/v1/tasks/{task_id}
# ---------------------------------------------------------------------------


class TestGetTask:
    @pytest.mark.asyncio
    async def test_get_existing(self, client: AsyncClient) -> None:
        create = await client.post("/a2a/v1/tasks", json=_task_body())
        task_id = create.json()["id"]

        resp = await client.get(f"/a2a/v1/tasks/{task_id}")
        assert resp.status_code == 200
        assert resp.json()["id"] == task_id

    @pytest.mark.asyncio
    async def test_get_not_found(self, client: AsyncClient) -> None:
        resp = await client.get("/a2a/v1/tasks/nonexistent")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /a2a/v1/tasks
# ---------------------------------------------------------------------------


class TestListTasks:
    @pytest.mark.asyncio
    async def test_list_empty(self, client: AsyncClient) -> None:
        resp = await client.get("/a2a/v1/tasks")
        assert resp.status_code == 200
        assert resp.json()["tasks"] == []

    @pytest.mark.asyncio
    async def test_list_with_tasks(self, client: AsyncClient) -> None:
        await client.post("/a2a/v1/tasks", json=_task_body("A"))
        await client.post("/a2a/v1/tasks", json=_task_body("B", msg_id="m2"))
        resp = await client.get("/a2a/v1/tasks")
        assert len(resp.json()["tasks"]) == 2

    @pytest.mark.asyncio
    async def test_filter_by_state(self, client: AsyncClient) -> None:
        await client.post("/a2a/v1/tasks", json=_task_body())
        resp = await client.get("/a2a/v1/tasks", params={"state": "completed"})
        tasks = resp.json()["tasks"]
        assert all(t["status"]["state"] == "completed" for t in tasks)

    @pytest.mark.asyncio
    async def test_pagination(self, client: AsyncClient) -> None:
        for i in range(3):
            await client.post("/a2a/v1/tasks", json=_task_body(f"T{i}", msg_id=f"m{i}"))
        resp = await client.get("/a2a/v1/tasks", params={"limit": 2})
        data = resp.json()
        assert len(data["tasks"]) == 2
        assert "nextCursor" in data


# ---------------------------------------------------------------------------
# POST /a2a/v1/tasks/{id}:cancel
# ---------------------------------------------------------------------------


class TestCancelTask:
    @pytest.mark.asyncio
    async def test_cancel_non_blocking(self, client: AsyncClient) -> None:
        create = await client.post("/a2a/v1/tasks", json=_task_body(blocking=False))
        task_id = create.json()["id"]
        resp = await client.post(f"/a2a/v1/tasks/{task_id}:cancel")
        # Task may already be completed
        assert resp.status_code in (200, 409)


# ---------------------------------------------------------------------------
# GET /a2a/v1/agent
# ---------------------------------------------------------------------------


class TestAgentEndpoint:
    @pytest.mark.asyncio
    async def test_get_agent(self, client: AsyncClient) -> None:
        resp = await client.get("/a2a/v1/agent")
        assert resp.status_code == 200
        assert resp.json()["name"] == "RestAgent"


# ---------------------------------------------------------------------------
# GET /.well-known/agent.json
# ---------------------------------------------------------------------------


class TestWellKnown:
    @pytest.mark.asyncio
    async def test_well_known(self, client: AsyncClient) -> None:
        resp = await client.get("/.well-known/agent.json")
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "RestAgent"
        assert data["protocolVersion"] == "0.3"
