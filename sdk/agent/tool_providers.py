"""Tool provider registry for system, MCP, and remote A2A tools."""

from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, cast

from sdk.a2a.client import A2AClient
from sdk.a2a.tool_adapter import register_remote_agent_as_tool
from sdk.agent.system_tools import get_system_tool_specs
from sdk.internal.tools import ToolRegistry

if TYPE_CHECKING:
    from sdk.agent.agents import Agent
    from sdk.mcp.types import MCPConnectionConfig

logger = logging.getLogger(__name__)


async def _register_tool(agent: Agent, tool_spec: Any) -> None:
    if agent.client is None:
        raise RuntimeError("Agent client is not initialized")
    register_result = cast(Any, agent.client.register_tool(tool_spec))
    if inspect.isawaitable(register_result):
        await cast(Any, register_result)


@dataclass
class ToolProviderContext:
    agent: Any


class ToolProvider(Protocol):
    async def install(self, context: ToolProviderContext) -> None:
        ...

    async def uninstall(self, context: ToolProviderContext) -> None:
        ...


class SystemToolProvider:
    async def install(self, context: ToolProviderContext) -> None:
        for tool_spec in get_system_tool_specs():
            await _register_tool(context.agent, tool_spec)

    async def uninstall(self, context: ToolProviderContext) -> None:
        return None


class MCPToolProvider:
    def __init__(self, configs: list[MCPConnectionConfig]) -> None:
        self._configs = configs

    async def install(self, context: ToolProviderContext) -> None:
        from sdk.backends.mcp_backend import MCPBackend

        backend = MCPBackend(self._configs)
        await backend.start()
        context.agent.mcp_backend = backend
        for tool_spec in backend.list_tools():
            await _register_tool(context.agent, tool_spec)

    async def uninstall(self, context: ToolProviderContext) -> None:
        backend = context.agent.mcp_backend
        if backend is not None:
            await backend.stop()
            context.agent.mcp_backend = None


class A2ARemoteToolProvider:
    def __init__(self, urls: list[str], auth_token: str | None = None) -> None:
        self._urls = urls
        self._auth_token = auth_token
        self._clients: list[A2AClient] = []

    async def install(self, context: ToolProviderContext) -> None:
        for url in self._urls:
            client = A2AClient(url, auth_token=self._auth_token)
            await client.connect()
            await client.discover()
            local_registry = ToolRegistry()
            spec = register_remote_agent_as_tool(local_registry, client)
            await _register_tool(context.agent, spec)
            self._clients.append(client)

    async def uninstall(self, context: ToolProviderContext) -> None:
        for client in self._clients:
            await client.disconnect()
        self._clients.clear()


class ToolProviderRegistry:
    def __init__(self) -> None:
        self._providers: list[ToolProvider] = []

    def add(self, provider: ToolProvider) -> None:
        self._providers.append(provider)

    async def install_all(self, context: ToolProviderContext) -> None:
        for provider in self._providers:
            await provider.install(context)

    async def uninstall_all(self, context: ToolProviderContext) -> None:
        for provider in reversed(self._providers):
            try:
                await provider.uninstall(context)
            except Exception as exc:
                logger.warning("Tool provider uninstall failed: %s", exc)
