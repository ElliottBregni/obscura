"""Tool providers for system, MCP, memory, and remote A2A tools.

Each provider registers tools on a ToolBroker via BrokerContext.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from obscura.core.tools import ToolRegistry
from obscura.integrations.a2a.client import A2AClient
from obscura.integrations.a2a.tool_adapter import register_remote_agent_as_tool
from obscura.tools.delegation import DelegationContext, make_task_tool
from obscura.tools.memory_tools import make_memory_tool_specs
from obscura.tools.system import get_system_tool_specs

if TYPE_CHECKING:
    from obscura.agent.agents import AgentConfig
    from obscura.auth.models import AuthenticatedUser
    from obscura.integrations.mcp.types import MCPConnectionConfig

logger = logging.getLogger(__name__)


@dataclass
class BrokerContext:
    """Context passed to providers during install/uninstall."""

    broker: Any  # ToolBroker
    agent: Any  # Agent — needed for lifecycle (e.g. MCP backend)
    allowed_tool_names: set[str] | None = None


class SystemToolProvider:
    async def install(self, context: BrokerContext) -> None:
        delegation_spec = self._build_delegation_tool(context)
        allowlist = self._get_tool_allowlist(context)

        for tool_spec in get_system_tool_specs():
            if allowlist is not None and tool_spec.name not in allowlist:
                continue
            if (
                context.allowed_tool_names is not None
                and tool_spec.name not in context.allowed_tool_names
            ):
                continue
            if tool_spec.name == "task" and delegation_spec is not None:
                context.broker.register_tool_spec(delegation_spec)
                continue
            context.broker.register_tool_spec(tool_spec)

    async def uninstall(self, context: BrokerContext) -> None:
        return None

    @staticmethod
    def _get_tool_allowlist(context: BrokerContext) -> list[str] | None:
        """Extract tool_allowlist from agent config, or None if unset."""
        try:
            config = getattr(context.agent, "config", None)
            if config is not None:
                allowlist: list[str] | None = getattr(config, "tool_allowlist", None)
                return allowlist
        except Exception:
            logger.debug("suppressed exception in _get_tool_allowlist", exc_info=True)
        return None

    @staticmethod
    def _build_delegation_tool(context: BrokerContext) -> Any:
        """Build a real delegation tool if the agent supports it."""
        try:
            agent = context.agent
            config: AgentConfig | None = getattr(agent, "config", None)
            if config is None or not config.can_delegate:
                return None

            peer_registry = getattr(
                getattr(agent, "runtime", None),
                "peer_registry",
                None,
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
            logger.debug(
                "suppressed exception in _build_delegation_tool", exc_info=True
            )
            return None


class MCPToolProvider:
    def __init__(self, configs: list[MCPConnectionConfig]) -> None:
        self._configs = configs

    async def install(self, context: BrokerContext) -> None:
        # lazy: avoid circular dep with obscura.agent.agents (transitively via providers/claude.py → integrations.mcp.server)
        from obscura.providers.mcp_backend import MCPBackend

        backend = MCPBackend(self._configs)
        await backend.start()
        context.agent.mcp_backend = backend
        for tool_spec in backend.list_tools():
            context.broker.register_tool_spec(tool_spec)

    async def uninstall(self, context: BrokerContext) -> None:
        backend = context.agent.mcp_backend
        if backend is not None:
            await backend.stop()
            context.agent.mcp_backend = None


class A2ARemoteToolProvider:
    def __init__(self, urls: list[str], auth_token: str | None = None) -> None:
        self._urls = urls
        self._auth_token = auth_token
        self._clients: list[A2AClient] = []

    async def install(self, context: BrokerContext) -> None:
        for url in self._urls:
            client = A2AClient(url, auth_token=self._auth_token)
            await client.connect()
            await client.discover()
            local_registry = ToolRegistry()
            spec = register_remote_agent_as_tool(local_registry, client)
            context.broker.register_tool_spec(spec)
            self._clients.append(client)

    async def uninstall(self, context: BrokerContext) -> None:
        for client in self._clients:
            await client.disconnect()
        self._clients.clear()


class MemoryToolProvider:
    """Provides memory and vector storage tools to agents."""

    async def install(self, context: BrokerContext) -> None:
        user: AuthenticatedUser | None = getattr(context.agent, "user", None)
        if user is None:
            logger.warning("MemoryToolProvider: No user found on agent, skipping")
            return

        for tool_spec in make_memory_tool_specs(user):
            context.broker.register_tool_spec(tool_spec)

    async def uninstall(self, context: BrokerContext) -> None:
        return None
