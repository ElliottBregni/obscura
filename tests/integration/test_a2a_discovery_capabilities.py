"""Integration tests for A2A discovery and capability integration."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from sdk.a2a.agent_card import AgentCardGenerator
from sdk.a2a.client import A2AClient
from sdk.a2a.service import A2AService
from sdk.a2a.store import InMemoryTaskStore
from sdk.a2a.transports.jsonrpc import create_jsonrpc_router
from sdk.a2a.transports.rest import create_rest_router, create_wellknown_router
from sdk.a2a.transports.sse import create_sse_router


def _build_app() -> FastAPI:
    card = (
        AgentCardGenerator("DiscoveryAgent", "https://a2a.local")
        .with_skills_from_tools(
            [
                {"name": "search_docs", "description": "Search internal docs", "tags": ["search"]},
                {"name": "triage_ticket", "description": "Classify support issues", "tags": ["support"]},
            ]
        )
        .with_capabilities(streaming=True, push_notifications=True, extended_card=True)
        .with_bearer_auth()
        .with_provider("Obscura", "https://obscura.dev")
        .build()
    )
    service = A2AService(store=InMemoryTaskStore(), agent_card=card)
    app = FastAPI()
    app.include_router(create_jsonrpc_router(service))
    app.include_router(create_rest_router(service))
    app.include_router(create_wellknown_router(service))
    app.include_router(create_sse_router(service))
    return app


@pytest.mark.integration
@pytest.mark.asyncio
async def test_well_known_discovery_exposes_skills_and_capabilities() -> None:
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        response = await http.get("/.well-known/agent.json")
        assert response.status_code == 200
        body = response.json()

    assert body["name"] == "DiscoveryAgent"
    assert body["protocolVersion"] == "0.3"
    assert len(body["skills"]) == 2
    skill_ids = {skill["id"] for skill in body["skills"]}
    assert {"search_docs", "triage_ticket"} == skill_ids
    assert body["capabilities"]["streaming"] is True
    assert body["capabilities"]["pushNotifications"] is True
    assert body["capabilities"]["extendedAgentCard"] is True


@pytest.mark.integration
@pytest.mark.asyncio
async def test_jsonrpc_agent_card_matches_well_known_discovery() -> None:
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        rpc_payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": "1",
            "method": "agent/authenticatedExtendedCard",
            "params": {},
        }
        rpc_response = await http.post("/a2a/rpc", json=rpc_payload)
        wk_response = await http.get("/.well-known/agent.json")
        assert rpc_response.status_code == 200
        assert wk_response.status_code == 200
        rpc_card = rpc_response.json()["result"]
        wk_card = wk_response.json()

    assert rpc_card["skills"] == wk_card["skills"]
    assert rpc_card["capabilities"] == wk_card["capabilities"]
    assert rpc_card["securitySchemes"] == wk_card["securitySchemes"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a2a_client_discover_populates_agent_card_capabilities() -> None:
    app = _build_app()
    client = A2AClient("http://test", transport=ASGITransport(app=app))
    await client.connect()
    try:
        card = await client.discover()
    finally:
        await client.disconnect()

    assert card.capabilities.streaming is True
    assert card.capabilities.pushNotifications is True
    assert card.capabilities.extendedAgentCard is True
    assert len(card.skills) == 2
