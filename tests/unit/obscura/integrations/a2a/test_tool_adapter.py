"""Tests for sdk.a2a.tool_adapter — registering remote agents as tools."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from obscura.integrations.a2a.agent_card import AgentCardGenerator
from obscura.integrations.a2a.client import A2AClient
from obscura.integrations.a2a.service import A2AService
from obscura.integrations.a2a.store import InMemoryTaskStore
from obscura.integrations.a2a.tool_adapter import register_remote_agent_as_tool
from obscura.integrations.a2a.transports.jsonrpc import create_jsonrpc_router
from obscura.integrations.a2a.transports.rest import create_wellknown_router
from obscura.core.tools import ToolRegistry


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def app() -> FastAPI:
    store = InMemoryTaskStore()
    card = AgentCardGenerator(
        "Remote Support Agent", "https://support.example.com",
        description="Handles customer support tickets",
    ).build()
    service = A2AService(store=store, agent_card=card)

    app = FastAPI()
    app.include_router(create_jsonrpc_router(service))
    app.include_router(create_wellknown_router(service))
    return app


@pytest.fixture
async def remote_client(app: FastAPI) -> AsyncGenerator[A2AClient, None]:
    import httpx

    transport = ASGITransport(app=app)
    client = A2AClient("http://test")
    client._http = httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"A2A-Version": "0.3"},
        timeout=30.0,
    )
    await client.discover()
    yield client
    await client.disconnect()


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestRegisterTool:
    @pytest.mark.asyncio
    async def test_registers_tool(self, remote_client: A2AClient) -> None:
        registry = ToolRegistry()
        spec = register_remote_agent_as_tool(registry, remote_client)
        assert spec.name == "remote_support_agent"
        assert registry.get("remote_support_agent") is not None

    @pytest.mark.asyncio
    async def test_custom_name(self, remote_client: A2AClient) -> None:
        registry = ToolRegistry()
        spec = register_remote_agent_as_tool(
            registry, remote_client, tool_name="support"
        )
        assert spec.name == "support"
        assert registry.get("support") is not None

    @pytest.mark.asyncio
    async def test_custom_description(self, remote_client: A2AClient) -> None:
        registry = ToolRegistry()
        spec = register_remote_agent_as_tool(
            registry, remote_client, description="Custom desc"
        )
        assert spec.description == "Custom desc"

    @pytest.mark.asyncio
    async def test_schema_has_message_param(self, remote_client: A2AClient) -> None:
        registry = ToolRegistry()
        spec = register_remote_agent_as_tool(registry, remote_client)
        assert "message" in spec.parameters["properties"]
        assert "message" in spec.parameters["required"]


# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------


class TestToolExecution:
    @pytest.mark.asyncio
    async def test_invoke_tool(self, remote_client: A2AClient) -> None:
        registry = ToolRegistry()
        spec = register_remote_agent_as_tool(registry, remote_client)
        result = await spec.handler(message="Test message")
        assert isinstance(result, str)
        assert len(result) > 0

    @pytest.mark.asyncio
    async def test_invoke_returns_artifact_text(self, remote_client: A2AClient) -> None:
        registry = ToolRegistry()
        spec = register_remote_agent_as_tool(registry, remote_client)
        result = await spec.handler(message="Process this request")
        # Placeholder mode returns the prompt text
        assert "Process this request" in result


# ---------------------------------------------------------------------------
# Registry integration
# ---------------------------------------------------------------------------


class TestRegistryIntegration:
    @pytest.mark.asyncio
    async def test_multiple_agents(self, remote_client: A2AClient) -> None:
        registry = ToolRegistry()
        register_remote_agent_as_tool(
            registry, remote_client, tool_name="agent_a"
        )
        register_remote_agent_as_tool(
            registry, remote_client, tool_name="agent_b"
        )
        assert len(registry) == 2
        assert registry.get("agent_a") is not None
        assert registry.get("agent_b") is not None

    @pytest.mark.asyncio
    async def test_tool_in_all_list(self, remote_client: A2AClient) -> None:
        registry = ToolRegistry()
        register_remote_agent_as_tool(registry, remote_client)
        names = registry.names()
        assert "remote_support_agent" in names
