"""Tests for sdk.agent.tool_providers."""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from obscura.tools.providers import (
    A2ARemoteToolProvider,
    BrokerContext,
    MCPToolProvider,
    SystemToolProvider,
)
from obscura.core.types import ToolSpec


@dataclass
class _FakeAgent:
    client: object
    mcp_backend: object | None = None


def _make_broker() -> MagicMock:
    """Create a mock ToolBroker."""
    broker = MagicMock()
    broker.register_tool_spec = MagicMock()
    return broker


class TestSystemToolProvider:
    @pytest.mark.asyncio
    async def test_registers_system_tools(self) -> None:
        broker = _make_broker()
        provider = SystemToolProvider()
        context = BrokerContext(
            broker=broker, agent=_FakeAgent(client=MagicMock()),
        )

        await provider.install(context)
        assert broker.register_tool_spec.call_count >= 2


class TestMCPToolProvider:
    @pytest.mark.asyncio
    async def test_starts_backend_and_registers_tools(self) -> None:
        mock_tool = ToolSpec(
            name="mcp_tool",
            description="demo",
            parameters={},
            handler=lambda: "ok",
        )
        broker = _make_broker()
        agent = _FakeAgent(client=MagicMock())

        with patch("obscura.providers.mcp_backend.MCPBackend") as MockBackend:
            backend = AsyncMock()
            backend.list_tools = MagicMock(return_value=[mock_tool])
            MockBackend.return_value = backend

            provider = MCPToolProvider(configs=[])
            context = BrokerContext(broker=broker, agent=agent)
            await provider.install(context)

            backend.start.assert_awaited_once()
            assert agent.mcp_backend is backend
            broker.register_tool_spec.assert_called_once_with(mock_tool)

            await provider.uninstall(context)
            backend.stop.assert_awaited_once()
            assert agent.mcp_backend is None


class TestA2ARemoteToolProvider:
    @pytest.mark.asyncio
    async def test_registers_remote_agents_as_tools(self) -> None:
        broker = _make_broker()
        agent = _FakeAgent(client=MagicMock())
        context = BrokerContext(broker=broker, agent=agent)

        spec = ToolSpec(
            name="remote_agent",
            description="remote",
            parameters={},
            handler=lambda: "ok",
        )

        with (
            patch("obscura.tools.providers.A2AClient") as MockA2AClient,
            patch(
                "obscura.tools.providers.register_remote_agent_as_tool",
                return_value=spec,
            ),
        ):
            remote_client = AsyncMock()
            MockA2AClient.return_value = remote_client

            provider = A2ARemoteToolProvider(
                urls=["https://remote-agent.local"],
                auth_token="token",
            )
            await provider.install(context)

            remote_client.connect.assert_awaited_once()
            remote_client.discover.assert_awaited_once()
            broker.register_tool_spec.assert_called_once_with(spec)

            await provider.uninstall(context)
            remote_client.disconnect.assert_awaited_once()
