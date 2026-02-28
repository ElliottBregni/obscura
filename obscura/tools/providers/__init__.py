"""Tool provider registry for system, MCP, and remote A2A tools."""

from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, cast

from obscura.integrations.a2a.client import A2AClient
from obscura.integrations.a2a.tool_adapter import register_remote_agent_as_tool
from obscura.tools.system import get_system_tool_specs
from obscura.core.tools import ToolRegistry

if TYPE_CHECKING:
    from obscura.agent.agents import Agent
    from obscura.integrations.mcp.types import MCPConnectionConfig

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
    async def install(self, context: ToolProviderContext) -> None: ...

    async def uninstall(self, context: ToolProviderContext) -> None: ...


class SystemToolProvider:
    async def install(self, context: ToolProviderContext) -> None:
        # Check if agent has delegation configured
        delegation_spec = self._build_delegation_tool(context)
        allowlist = self._get_tool_allowlist(context)

        for tool_spec in get_system_tool_specs():
            # Skip tools not in allowlist (if configured)
            if allowlist is not None and tool_spec.name not in allowlist:
                continue
            # Replace stub task tool with real delegation tool if available
            if tool_spec.name == "task" and delegation_spec is not None:
                await _register_tool(context.agent, delegation_spec)
                continue
            await _register_tool(context.agent, tool_spec)

    async def uninstall(self, context: ToolProviderContext) -> None:
        return None

    @staticmethod
    def _get_tool_allowlist(context: ToolProviderContext) -> list[str] | None:
        """Extract tool_allowlist from agent config, or None if unset."""
        try:
            config = getattr(context.agent, "config", None)
            if config is not None:
                allowlist: list[str] | None = getattr(config, "tool_allowlist", None)
                return allowlist
        except Exception:
            pass
        return None

    @staticmethod
    def _build_delegation_tool(context: ToolProviderContext) -> Any:
        """Build a real delegation tool if the agent supports it."""
        try:
            from obscura.agent.agents import AgentConfig
            from obscura.tools.delegation import DelegationContext, make_task_tool

            agent = context.agent
            config: AgentConfig | None = getattr(agent, "config", None)
            if config is None or not config.can_delegate:
                return None

            peer_registry = getattr(
                getattr(agent, "runtime", None), "peer_registry", None
            )

            ctx = DelegationContext(
                peer_registry=peer_registry,
                can_delegate=config.can_delegate,
                delegate_allowlist=list(config.delegate_allowlist),
                max_delegation_depth=config.max_delegation_depth,
                caller_agent_id=getattr(agent, "id", ""),
            )
            return make_task_tool(ctx)
        except Exception:
            return None


class MCPToolProvider:
    def __init__(self, configs: list[MCPConnectionConfig]) -> None:
        self._configs = configs

    async def install(self, context: ToolProviderContext) -> None:
        from obscura.providers.mcp_backend import MCPBackend

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
