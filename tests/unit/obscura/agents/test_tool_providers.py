"""Tests for sdk.agent.tool_providers."""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from obscura.tools.providers import (
    A2ARemoteToolProvider,
    MCPToolProvider,
    SystemToolProvider,
    ToolProviderContext,
    ToolProviderRegistry,
)
from obscura.core.types import ToolSpec


@dataclass
class _FakeAgent:
    client: object
    mcp_backend: object | None = None


class TestToolProviderRegistry:
    @pytest.mark.asyncio
    async def test_install_and_uninstall_all(self) -> None:
        events: list[str] = []

        class _Provider:
            def __init__(self, name: str) -> None:
                self._name = name

            async def install(self, context: ToolProviderContext) -> None:
                _ = context
                events.append(f"install:{self._name}")

            async def uninstall(self, context: ToolProviderContext) -> None:
                _ = context
                events.append(f"uninstall:{self._name}")

        registry = ToolProviderRegistry()
        registry.add(_Provider("one"))
        registry.add(_Provider("two"))
        context = ToolProviderContext(agent=_FakeAgent(client=MagicMock()))
        await registry.install_all(context)
        await registry.uninstall_all(context)

        assert events == [
            "install:one",
            "install:two",
            "uninstall:two",
            "uninstall:one",
        ]


class TestSystemToolProvider:
    @pytest.mark.asyncio
    async def test_registers_system_tools(self) -> None:
        client = MagicMock()
        client.register_tool = MagicMock()
        provider = SystemToolProvider()
        context = ToolProviderContext(agent=_FakeAgent(client=client))

        await provider.install(context)
        assert client.register_tool.call_count >= 2


class TestMCPToolProvider:
    @pytest.mark.asyncio
    async def test_starts_backend_and_registers_tools(self) -> None:
        mock_tool = ToolSpec(
            name="mcp_tool",
            description="demo",
            parameters={},
            handler=lambda: "ok",
        )
        client = MagicMock()
        client.register_tool = MagicMock()
        agent = _FakeAgent(client=client)

        with patch("obscura.providers.mcp_backend.MCPBackend") as MockBackend:
            backend = AsyncMock()
            backend.list_tools = MagicMock(return_value=[mock_tool])
            MockBackend.return_value = backend

            provider = MCPToolProvider(configs=[])
            context = ToolProviderContext(agent=agent)
            await provider.install(context)

            backend.start.assert_awaited_once()
            assert agent.mcp_backend is backend
            client.register_tool.assert_called_once_with(mock_tool)

            await provider.uninstall(context)
            backend.stop.assert_awaited_once()
            assert agent.mcp_backend is None


class TestA2ARemoteToolProvider:
    @pytest.mark.asyncio
    async def test_registers_remote_agents_as_tools(self) -> None:
        client = MagicMock()
        client.register_tool = MagicMock()
        agent = _FakeAgent(client=client)
        context = ToolProviderContext(agent=agent)

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
            client.register_tool.assert_called_once_with(spec)

            await provider.uninstall(context)
            remote_client.disconnect.assert_awaited_once()
