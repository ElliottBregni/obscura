"""Tests for sdk.a2a.transports.sse — SSE streaming transport.

Since httpx doesn't natively consume SSE, we test by verifying the
endpoint returns 200 and the correct content type.
For full SSE parsing we'd use httpx-sse, but we keep tests simple
and validate the EventSourceResponse wiring.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from sdk.a2a.agent_card import AgentCardGenerator
from sdk.a2a.service import A2AService
from sdk.a2a.store import InMemoryTaskStore
from sdk.a2a.transports.sse import create_sse_router


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def app() -> FastAPI:
    store = InMemoryTaskStore()
    card = AgentCardGenerator("SSEAgent", "https://test.local").build()
    service = A2AService(store=store, agent_card=card)

    app = FastAPI()
    app.include_router(create_sse_router(service))
    return app


@pytest.fixture
async def client(app: FastAPI) -> AsyncClient:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c  # type: ignore[misc]


# ---------------------------------------------------------------------------
# POST /a2a/v1/tasks/streaming
# ---------------------------------------------------------------------------


class TestStreamTask:
    @pytest.mark.asyncio
    async def test_streaming_returns_200(self, client: AsyncClient) -> None:
        resp = await client.post("/a2a/v1/tasks/streaming", json={
            "message": {
                "role": "user",
                "messageId": "m1",
                "parts": [{"kind": "text", "text": "Stream me"}],
            },
        })
        assert resp.status_code == 200
        # SSE content type
        assert "text/event-stream" in resp.headers.get("content-type", "")

    @pytest.mark.asyncio
    async def test_streaming_contains_events(self, client: AsyncClient) -> None:
        resp = await client.post("/a2a/v1/tasks/streaming", json={
            "message": {
                "role": "user",
                "messageId": "m1",
                "parts": [{"kind": "text", "text": "Hello"}],
            },
        })
        body = resp.text
        # Should contain SSE event fields
        assert "event:" in body
        assert "data:" in body

    @pytest.mark.asyncio
    async def test_streaming_has_status_update(self, client: AsyncClient) -> None:
        resp = await client.post("/a2a/v1/tasks/streaming", json={
            "message": {
                "role": "user",
                "messageId": "m1",
                "parts": [{"kind": "text", "text": "Process"}],
            },
        })
        body = resp.text
        assert "status-update" in body

    @pytest.mark.asyncio
    async def test_with_context_id(self, client: AsyncClient) -> None:
        resp = await client.post("/a2a/v1/tasks/streaming", json={
            "message": {
                "role": "user",
                "messageId": "m1",
                "parts": [{"kind": "text", "text": "Test"}],
            },
            "contextId": "ctx-stream",
        })
        assert resp.status_code == 200
        assert "ctx-stream" in resp.text


# ---------------------------------------------------------------------------
# POST /a2a/v1/tasks/{id}:subscribe
# ---------------------------------------------------------------------------


class TestSubscribeTask:
    @pytest.mark.asyncio
    async def test_subscribe_not_found(self, client: AsyncClient) -> None:
        """Subscribe to a non-existent task should return an error event."""
        resp = await client.post("/a2a/v1/tasks/nonexistent:subscribe")
        assert resp.status_code == 200  # SSE always returns 200
        assert "error" in resp.text
