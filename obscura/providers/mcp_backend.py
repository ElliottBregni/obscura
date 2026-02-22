"""
obscura.backends.mcp_backend — Backend that uses MCP tools/resources.

Allows agents to use tools from external MCP servers.

Usage::

    from obscura.providers.mcp_backend import MCPBackend
    from obscura.integrations.mcp.types import MCPConnectionConfig, MCPTransportType

    backend = MCPBackend(
        mcp_servers=[
            MCPConnectionConfig(
                transport=MCPTransportType.STDIO,
                command="npx",
                args=["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
            ),
        ],
    )

    await backend.start()

    # Use like any backend
    message = await backend.send("Read the file /tmp/test.txt")
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import TYPE_CHECKING, Any, AsyncIterator, Callable

if TYPE_CHECKING:
    from obscura.core.tools import ToolRegistry

from obscura.core.types import (
    HookContext,
    HookPoint,
    Message,
    SessionRef,
    StreamChunk,
    ToolSpec,
)
from obscura.integrations.mcp.client import MCPSessionManager
from obscura.integrations.mcp.tools import mcp_result_to_obscura
from obscura.integrations.mcp.types import MCPConnectionConfig, MCPError

logger = logging.getLogger(__name__)


class MCPBackend:
    """
    BackendProtocol implementation that uses MCP servers for tools.

    This backend doesn't have its own LLM - it expects to be used
    as a tool provider for other backends, or for direct tool calls.
    """

    def __init__(
        self,
        mcp_servers: list[MCPConnectionConfig] | None = None,
        name: str = "mcp",
    ):
        from obscura.core.tools import ToolRegistry

        self.name = name
        self.mcp_servers = mcp_servers or []
        self._session_manager = MCPSessionManager()
        self._tools: list[ToolSpec] = []
        self._tool_registry = ToolRegistry()
        self._hooks: dict[HookPoint, list[Callable[..., Any]]] = {
            hp: [] for hp in HookPoint
        }
        self._initialized = False

    # -- Testing/observability accessors ------------------------------------

    @property
    def tools(self) -> list[ToolSpec]:
        return self._tools

    @property
    def hooks(self) -> dict[HookPoint, list[Callable[..., Any]]]:
        return self._hooks

    @property
    def tool_registry(self) -> ToolRegistry:
        return self._tool_registry

    @property
    def initialized(self) -> bool:
        return self._initialized

    @property
    def session_manager(self) -> MCPSessionManager:
        return self._session_manager

    # -- Lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        """Initialize the MCP backend and connect to all servers."""
        if self._initialized:
            return

        # Connect to all MCP servers
        for i, config in enumerate(self.mcp_servers):
            session_name = f"mcp_server_{i}"
            try:
                await self._session_manager.add_session(session_name, config)
                logger.info(f"Connected to MCP server: {session_name}")
            except Exception as e:
                logger.error(f"Failed to connect to MCP server {session_name}: {e}")

        # Aggregate tools from all servers
        await self._refresh_tools()

        self._initialized = True
        logger.info("MCP backend initialized")

    async def stop(self) -> None:
        """Stop the MCP backend and disconnect from all servers."""
        await self._session_manager.close_all()
        self._tools = []
        self._initialized = False
        logger.info("MCP backend stopped")

    async def _refresh_tools(self) -> None:
        """Refresh the list of available tools from all servers."""
        from obscura.core.tools import ToolRegistry

        self._tools = []
        self._tool_registry = ToolRegistry()

        mcp_tools = await self._session_manager.aggregate_tools()

        for mcp_tool in mcp_tools:
            tool_spec = self._mcp_tool_to_obscura(mcp_tool)
            self._tools.append(tool_spec)
            self._tool_registry.register(tool_spec)

        logger.info(f"Loaded {len(self._tools)} tools from MCP servers")

    def _mcp_tool_to_obscura(self, mcp_tool: Any) -> ToolSpec:
        """Convert an MCP tool to Obscura ToolSpec."""
        # Parse the prefixed name: "session_name.tool_name"
        name_parts = mcp_tool.name.split(".", 1)
        if len(name_parts) == 2:
            session_name, tool_name = name_parts
        else:
            session_name = "unknown"
            tool_name = mcp_tool.name

        async def tool_handler(**kwargs: Any) -> Any:
            """Execute the MCP tool."""
            return await self._execute_mcp_tool(session_name, tool_name, kwargs)

        return ToolSpec(
            name=mcp_tool.name,
            description=mcp_tool.description,
            parameters=mcp_tool.inputSchema,
            handler=tool_handler,
        )

    async def _execute_mcp_tool(
        self,
        session_name: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> Any:
        """Execute an MCP tool via the session manager."""
        client = self._session_manager.get_session(session_name)
        if client is None:
            raise MCPError(
                code=-32000,
                message=f"MCP session not found: {session_name}",
            )

        result = await client.call_tool(tool_name, arguments)
        return mcp_result_to_obscura(result)

    # -- BackendProtocol Implementation --------------------------------------

    async def send(self, prompt: str, **kwargs: Any) -> Message:
        """
        Send a prompt - not supported directly.

        This backend is for tools only, not direct LLM calls.
        """
        raise NotImplementedError(
            "MCPBackend does not support direct LLM calls. "
            "Use it as a tool provider for other backends."
        )

    async def stream(self, prompt: str, **kwargs: Any) -> AsyncIterator[StreamChunk]:
        """
        Stream a response - not supported directly.

        This backend is for tools only, not direct LLM calls.
        """
        raise NotImplementedError(
            "MCPBackend does not support direct LLM calls. "
            "Use it as a tool provider for other backends."
        )

    # -- Sessions (not supported) --------------------------------------------

    async def create_session(self, **kwargs: Any) -> SessionRef:
        """Create a session - not supported by MCP backend."""
        raise NotImplementedError("MCPBackend does not support sessions")

    async def resume_session(self, ref: SessionRef) -> None:
        """Resume a session - not supported by MCP backend."""
        raise NotImplementedError("MCPBackend does not support sessions")

    async def list_sessions(self) -> list[SessionRef]:
        """List sessions - not supported by MCP backend."""
        return []

    async def delete_session(self, ref: SessionRef) -> None:
        """Delete a session - not supported by MCP backend."""
        raise NotImplementedError("MCPBackend does not support sessions")

    # -- Tools ---------------------------------------------------------------

    def register_tool(self, spec: ToolSpec) -> None:
        """
        Register a tool.

        Note: Tools are automatically loaded from MCP servers.
        Manually registered tools will be added to the list.
        """
        self._tools.append(spec)
        self._tool_registry.register(spec)
        logger.debug(f"Registered tool: {spec.name}")

    def get_tool_registry(self) -> ToolRegistry:
        """Return the tool registry."""
        return self._tool_registry

    def list_tools(self) -> list[ToolSpec]:
        """List all available tools from MCP servers."""
        return self._tools.copy()

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """Call a tool by name."""
        # Find the tool
        tool = None
        for t in self._tools:
            if t.name == name:
                tool = t
                break

        if tool is None:
            raise MCPError(
                code=-32003,
                message=f"Tool not found: {name}",
            )

        # Execute hooks
        context = HookContext(
            hook=HookPoint.PRE_TOOL_USE,
            tool_name=name,
            tool_input=arguments,
        )
        await self._run_hooks(context)

        # Execute tool
        result = await tool.handler(**arguments)

        # Post-execution hooks
        context = HookContext(
            hook=HookPoint.POST_TOOL_USE,
            tool_name=name,
            tool_input=arguments,
            tool_output=result,
        )
        await self._run_hooks(context)

        return result

    # -- Hooks ---------------------------------------------------------------

    def register_hook(self, hook: HookPoint, callback: Callable[..., Any]) -> None:
        """Register a hook callback."""
        if hook not in self._hooks:
            self._hooks[hook] = []
        self._hooks[hook].append(callback)

    async def _run_hooks(self, context: HookContext) -> None:
        """Run all registered hooks for a given point."""
        callbacks = self._hooks.get(context.hook, [])
        for callback in callbacks:
            try:
                if inspect.iscoroutinefunction(callback):
                    await callback(context)
                else:
                    callback(context)
            except Exception as e:
                logger.warning(f"Hook failed: {e}")

    # -- Additional Methods --------------------------------------------------

    async def add_server(self, config: MCPConnectionConfig) -> str:
        """
        Add a new MCP server dynamically.

        Args:
            config: Connection configuration for the server

        Returns:
            Session name for the new server
        """
        session_name = f"mcp_server_{len(self._session_manager.list_sessions())}"
        await self._session_manager.add_session(session_name, config)

        # Refresh tools
        await self._refresh_tools()

        return session_name

    async def remove_server(self, session_name: str) -> None:
        """
        Remove an MCP server.

        Args:
            session_name: Name of the session to remove
        """
        await self._session_manager.remove_session(session_name)

        # Refresh tools
        await self._refresh_tools()

    def list_servers(self) -> list[str]:
        """List all connected MCP server session names."""
        return self._session_manager.list_sessions()

    async def health_check(self) -> dict[str, Any]:
        """
        Check health of all MCP connections.

        Returns:
            Health status for each server
        """
        health: dict[str, dict[str, str]] = {}

        for name in self._session_manager.list_sessions():
            client = self._session_manager.get_session(name)
            if client is None:
                health[name] = {"status": "disconnected"}
                continue

            try:
                # Try to ping the server
                await asyncio.wait_for(client.ping(), timeout=5.0)
                health[name] = {"status": "healthy"}
            except Exception as e:
                health[name] = {"status": "unhealthy", "error": str(e)}

        return health


class MCPBackendMixin:
    """
    Mixin to add MCP capabilities to any backend.

    This allows existing backends (Copilot, Claude) to use MCP tools
    alongside their native capabilities.

    Usage::

        class CopilotWithMCP(CopilotBackend, MCPBackendMixin):
            def __init__(self, *args, mcp_servers=None, **kwargs):
                super().__init__(*args, **kwargs)
                MCPBackendMixin.__init__(self, mcp_servers)
    """

    def __init__(self, mcp_servers: list[MCPConnectionConfig] | None = None) -> None:
        self._mcp_backend = MCPBackend(mcp_servers or [])
        self._mcp_tools_added = False

    def _parent_register_tool(self, spec: ToolSpec) -> None:
        """Call register_tool on the next class in MRO (the concrete backend)."""
        # super() resolves at runtime via MRO to the concrete backend's register_tool.
        register: Any = getattr(super(), "register_tool")
        register(spec)

    async def start(self) -> None:
        """Start the backend and MCP tools."""
        await self._mcp_backend.start()

        if not self._mcp_tools_added:
            for tool in self._mcp_backend.list_tools():
                self._parent_register_tool(tool)
            self._mcp_tools_added = True

    async def stop(self) -> None:
        """Stop the backend and MCP tools."""
        await self._mcp_backend.stop()

    def register_mcp_tool(self, tool_spec: ToolSpec) -> None:
        """Register a tool from MCP backend."""
        self._parent_register_tool(tool_spec)
