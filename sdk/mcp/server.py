"""
sdk.mcp.server — FastMCP-based MCP server for Obscura.

Exposes Obscura functionality as MCP tools, resources, and prompts.
Can run alongside the main FastAPI server.

Usage::

    from sdk.mcp.server import ObscuraMCPServer
    
    server = ObscuraMCPServer()
    
    # Run via stdio (for MCP clients)
    await server.run_stdio()
    
    # Or get the FastMCP app for mounting in FastAPI
    app = server.get_app()
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from sdk.agents import AgentConfig, AgentRuntime
from sdk.auth.models import AuthenticatedUser
from sdk.memory import MemoryStore
from sdk.mcp.tools import (
    create_array_property,
    create_boolean_property,
    create_string_property,
    get_obscura_mcp_registry,
)
from sdk.mcp.types import (
    MCPError,
    MCPErrorCode,
    MCPToolResult,
    ObscuraMCPToolContext,
    ObscuraMCPConfig,
)
from sdk.agents import AgentConfig

logger = logging.getLogger(__name__)


class ObscuraMCPServer:
    """
    MCP server for Obscura.
    
    Exposes Obscura functionality via the Model Context Protocol.
    Supports stdio and SSE transports.
    """
    
    def __init__(
        self,
        config: ObscuraMCPConfig | None = None,
        user: AuthenticatedUser | None = None,
    ):
        self.config = config or ObscuraMCPConfig()
        self.user = user
        self._runtime: AgentRuntime | None = None
        self._registry = get_obscura_mcp_registry()
        self._initialized = False
        
        # Register Obscura-specific tools
        self._register_obscura_tools()
    
    def _register_obscura_tools(self) -> None:
        """Register all Obscura MCP tools."""
        # list_agents tool
        self._registry.register(
            name="list_agents",
            description="List all active agents with their status",
            parameters={
                "properties": {
                    "namespace": create_string_property(
                        "Optional namespace filter",
                        default="default",
                    ),
                },
                "required": [],
            },
            handler=self._handle_list_agents,
        )
        
        # spawn_agent tool
        self._registry.register(
            name="spawn_agent",
            description="Spawn a new agent with the given configuration",
            parameters={
                "properties": {
                    "name": create_string_property("Agent name"),
                    "model": create_string_property(
                        "Model to use",
                        enum=["copilot", "claude"],
                        default="copilot",
                    ),
                    "system_prompt": create_string_property(
                        "System prompt for the agent",
                        default="",
                    ),
                    "memory_namespace": create_string_property(
                        "Memory namespace",
                        default="default",
                    ),
                    "tags": create_array_property(
                        "Tags for the agent",
                        items={"type": "string"},
                    ),
                },
                "required": ["name", "model"],
            },
            handler=self._handle_spawn_agent,
        )
        
        # stop_agent tool
        self._registry.register(
            name="stop_agent",
            description="Stop an agent by ID",
            parameters={
                "properties": {
                    "agent_id": create_string_property("Agent ID to stop"),
                    "force": create_boolean_property(
                        "Force stop immediately",
                        default=False,
                    ),
                },
                "required": ["agent_id"],
            },
            handler=self._handle_stop_agent,
        )
        
        # get_memory tool
        self._registry.register(
            name="get_memory",
            description="Retrieve a value from memory",
            parameters={
                "properties": {
                    "key": create_string_property("Memory key"),
                    "namespace": create_string_property(
                        "Memory namespace",
                        default="default",
                    ),
                },
                "required": ["key"],
            },
            handler=self._handle_get_memory,
        )
        
        # set_memory tool
        self._registry.register(
            name="set_memory",
            description="Store a value in memory",
            parameters={
                "properties": {
                    "key": create_string_property("Memory key"),
                    "value": create_string_property("Value to store (JSON string)"),
                    "namespace": create_string_property(
                        "Memory namespace",
                        default="default",
                    ),
                },
                "required": ["key", "value"],
            },
            handler=self._handle_set_memory,
        )
        
        # delete_memory tool
        self._registry.register(
            name="delete_memory",
            description="Delete a key from memory",
            parameters={
                "properties": {
                    "key": create_string_property("Memory key to delete"),
                    "namespace": create_string_property(
                        "Memory namespace",
                        default="default",
                    ),
                },
                "required": ["key"],
            },
            handler=self._handle_delete_memory,
        )
        
        # search_memory tool
        self._registry.register(
            name="search_memory",
            description="Search memory for keys or values matching a query",
            parameters={
                "properties": {
                    "query": create_string_property("Search query"),
                    "namespace": create_string_property(
                        "Optional namespace filter",
                    ),
                },
                "required": ["query"],
            },
            handler=self._handle_search_memory,
        )
        
        # get_agent_status tool
        self._registry.register(
            name="get_agent_status",
            description="Get detailed status of an agent",
            parameters={
                "properties": {
                    "agent_id": create_string_property("Agent ID"),
                },
                "required": ["agent_id"],
            },
            handler=self._handle_get_agent_status,
        )
    
    # -----------------------------------------------------------------------
    # Tool Handlers
    # -----------------------------------------------------------------------
    
    async def _handle_list_agents(
        self,
        context: ObscuraMCPToolContext,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """Handle list_agents tool call."""
        if self._runtime is None:
            return {"agents": [], "error": "Agent runtime not initialized"}
        
        namespace = arguments.get("namespace")
        agents = self._runtime.list_agents()
        
        result: list[dict[str, Any]] = []
        for agent in agents:
            agent_data = {
                "id": agent.id,
                "name": agent.config.name,
                "status": agent.status.name,
                "model": agent.config.model,
                "memory_namespace": agent.config.memory_namespace,
                "created_at": agent.created_at.isoformat(),
                "iteration_count": agent.iteration_count,
            }
            if namespace and agent.config.memory_namespace != namespace:
                continue
            result.append(agent_data)
        
        return {"agents": result, "count": len(result)}
    
    async def _handle_spawn_agent(
        self,
        context: ObscuraMCPToolContext,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """Handle spawn_agent tool call."""
        if self._runtime is None:
            return {"error": "Agent runtime not initialized"}
        
        if self.user is None:
            return {"error": "User not authenticated"}
        
        config = AgentConfig(
            name=arguments["name"],
            model=arguments["model"],
            system_prompt=arguments.get("system_prompt", ""),
            memory_namespace=arguments.get("memory_namespace", "default"),
            tags=arguments.get("tags", []),
        )
        
        agent = self._runtime.spawn(
            name=config.name,
            model=config.model,
            system_prompt=config.system_prompt,
            memory_namespace=config.memory_namespace,
        )
        await agent.start()
        
        return {
            "agent_id": agent.id,
            "name": agent.config.name,
            "status": agent.status.name,
            "created_at": agent.created_at.isoformat(),
        }
    
    async def _handle_stop_agent(
        self,
        context: ObscuraMCPToolContext,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """Handle stop_agent tool call."""
        if self._runtime is None:
            return {"error": "Agent runtime not initialized"}
        
        agent_id = arguments["agent_id"]
        force = arguments.get("force", False)
        
        agent = self._runtime.get_agent(agent_id)
        if agent is None:
            return {"error": f"Agent not found: {agent_id}"}
        
        if force:
            await agent.stop()
        else:
            await agent.stop_graceful()
        
        return {
            "agent_id": agent_id,
            "status": agent.status.name,
            "stopped": True,
        }
    
    async def _handle_get_memory(
        self,
        context: ObscuraMCPToolContext,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """Handle get_memory tool call."""
        if self.user is None:
            return {"error": "User not authenticated"}
        
        key = arguments["key"]
        namespace = arguments.get("namespace", "default")
        
        memory = MemoryStore.for_user(self.user)
        value = memory.get(key, namespace=namespace)
        
        if value is None:
            return {"found": False, "key": key, "namespace": namespace}
        
        return {
            "found": True,
            "key": key,
            "namespace": namespace,
            "value": value,
        }
    
    async def _handle_set_memory(
        self,
        context: ObscuraMCPToolContext,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """Handle set_memory tool call."""
        if self.user is None:
            return {"error": "User not authenticated"}
        
        key = arguments["key"]
        value_str = arguments["value"]
        namespace = arguments.get("namespace", "default")
        
        # Parse value as JSON if possible
        try:
            value = json.loads(value_str)
        except json.JSONDecodeError:
            value = value_str
        
        memory = MemoryStore.for_user(self.user)
        memory.set(key, value, namespace=namespace)
        
        return {
            "success": True,
            "key": key,
            "namespace": namespace,
        }
    
    async def _handle_delete_memory(
        self,
        context: ObscuraMCPToolContext,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """Handle delete_memory tool call."""
        if self.user is None:
            return {"error": "User not authenticated"}
        
        key = arguments["key"]
        namespace = arguments.get("namespace", "default")
        
        memory = MemoryStore.for_user(self.user)
        deleted = memory.delete(key, namespace=namespace)
        
        return {
            "success": deleted,
            "key": key,
            "namespace": namespace,
        }
    
    async def _handle_search_memory(
        self,
        context: ObscuraMCPToolContext,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """Handle search_memory tool call."""
        if self.user is None:
            return {"error": "User not authenticated"}
        
        query = arguments["query"]
        namespace = arguments.get("namespace")
        
        memory = MemoryStore.for_user(self.user)
        
        if namespace:
            # Search within specific namespace
            keys = memory.list_keys(namespace=namespace)
            results: list[dict[str, Any]] = []
            for mk in keys:
                if query.lower() in mk.key.lower():
                    value = memory.get(mk.key, namespace=mk.namespace)
                    results.append({
                        "key": mk.key,
                        "namespace": mk.namespace,
                        "value": value,
                    })
        else:
            # Search all namespaces
            search_results = memory.search(query)
            results = [
                {
                    "key": str(key),
                    "value": value,
                }
                for key, value in search_results
            ]
        
        return {
            "results": results,
            "count": len(results),
        }
    
    async def _handle_get_agent_status(
        self,
        context: ObscuraMCPToolContext,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """Handle get_agent_status tool call."""
        if self._runtime is None:
            return {"error": "Agent runtime not initialized"}
        
        agent_id = arguments["agent_id"]
        agent = self._runtime.get_agent(agent_id)
        
        if agent is None:
            return {"error": f"Agent not found: {agent_id}"}
        
        return {
            "agent_id": agent.id,
            "name": agent.config.name,
            "status": agent.status.name,
            "model": agent.config.model,
            "memory_namespace": agent.config.memory_namespace,
            "created_at": agent.created_at.isoformat(),
            "updated_at": agent.updated_at.isoformat(),
            "iteration_count": agent.iteration_count,
            "tags": agent.config.tags,
        }
    
    # -----------------------------------------------------------------------
    # Server Lifecycle
    # -----------------------------------------------------------------------
    
    async def initialize(self, user: AuthenticatedUser | None = None) -> None:
        """Initialize the MCP server."""
        if self._initialized:
            return
        
        if user:
            self.user = user
        
        if self.user:
            self._runtime = AgentRuntime(self.user)
            await self._runtime.start()
        
        self._initialized = True
        logger.info("Obscura MCP server initialized")
    
    async def shutdown(self) -> None:
        """Shutdown the MCP server."""
        if self._runtime:
            await self._runtime.stop()
            self._runtime = None
        
        self._initialized = False
        logger.info("Obscura MCP server shutdown")
    
    # -----------------------------------------------------------------------
    # MCP Protocol Methods
    # -----------------------------------------------------------------------
    
    async def handle_initialize(
        self,
        protocolVersion: str,
        capabilities: dict[str, Any],
        clientInfo: dict[str, str],
    ) -> dict[str, Any]:
        """Handle MCP initialize request."""
        await self.initialize()
        
        return {
            "protocolVersion": "2024-11-05",
            "capabilities": {
                "tools": {},
                "resources": {},
                "prompts": {},
            },
            "serverInfo": {
                "name": "obscura-mcp",
                "version": "0.2.0",
            },
        }
    
    async def handle_tools_list(self) -> list[dict[str, Any]]:
        """Handle MCP tools/list request."""
        tools = self._registry.list_tools()
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "inputSchema": tool.inputSchema,
            }
            for tool in tools
        ]
    
    async def handle_tools_call(
        self,
        name: str,
        arguments: dict[str, Any],
        context: ObscuraMCPToolContext | None = None,
    ) -> MCPToolResult:
        """Handle MCP tools/call request."""
        if context is None:
            context = ObscuraMCPToolContext(user_id="anonymous")
        
        return await self._registry.execute(name, context, arguments)
    
    async def handle_resources_list(self) -> list[dict[str, Any]]:
        """Handle MCP resources/list request."""
        # Return memory namespaces as resources
        resources: list[dict[str, Any]] = []
        
        if self.user:
            memory = MemoryStore.for_user(self.user)
            namespaces: set[str] = set()
            for key in memory.list_keys():
                namespaces.add(key.namespace)
            
            for ns in sorted(namespaces):
                resources.append({
                    "uri": f"memory://{ns}",
                    "name": f"Memory namespace: {ns}",
                    "mimeType": "application/json",
                })
        
        return resources
    
    async def handle_resources_read(self, uri: str) -> dict[str, Any]:
        """Handle MCP resources/read request."""
        if uri.startswith("memory://"):
            parts = uri[9:].split("/", 1)
            namespace = parts[0]
            key = parts[1] if len(parts) > 1 else None
            
            if self.user is None:
                raise MCPError(
                    code=MCPErrorCode.RESOURCE_ACCESS_DENIED.value,
                    message="User not authenticated",
                )
            
            memory = MemoryStore.for_user(self.user)
            
            if key:
                value = memory.get(key, namespace=namespace)
                if value is None:
                    raise MCPError(
                        code=MCPErrorCode.RESOURCE_NOT_FOUND.value,
                        message=f"Resource not found: {uri}",
                    )
                
                return {
                    "contents": [
                        {
                            "uri": uri,
                            "mimeType": "application/json",
                            "text": json.dumps(value, default=str),
                        }
                    ]
                }
            else:
                # List all keys in namespace
                keys = memory.list_keys(namespace=namespace)
                return {
                    "contents": [
                        {
                            "uri": uri,
                            "mimeType": "application/json",
                            "text": json.dumps({
                                "namespace": namespace,
                                "keys": [k.key for k in keys],
                            }),
                        }
                    ]
                }
        
        raise MCPError(
            code=MCPErrorCode.RESOURCE_NOT_FOUND.value,
            message=f"Unknown resource URI: {uri}",
        )
    
    async def handle_prompts_list(self) -> list[dict[str, Any]]:
        """Handle MCP prompts/list request."""
        prompts = [
            {
                "name": "agent_task",
                "description": "Template for agent task execution",
                "arguments": [
                    {
                        "name": "task",
                        "description": "The task to execute",
                        "required": True,
                    }
                ],
            },
            {
                "name": "memory_query",
                "description": "Template for memory-based queries",
                "arguments": [
                    {
                        "name": "query",
                        "description": "The query to search for in memory",
                        "required": True,
                    }
                ],
            },
        ]
        return prompts
    
    async def handle_prompts_get(
        self,
        name: str,
        arguments: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Handle MCP prompts/get request."""
        arguments = arguments or {}
        
        if name == "agent_task":
            task = arguments.get("task", "Execute the task")
            return {
                "description": f"Agent task: {task}",
                "messages": [
                    {
                        "role": "user",
                        "content": {
                            "type": "text",
                            "text": f"Please execute this task: {task}",
                        },
                    }
                ],
            }
        elif name == "memory_query":
            query = arguments.get("query", "")
            return {
                "description": f"Memory query: {query}",
                "messages": [
                    {
                        "role": "user",
                        "content": {
                            "type": "text",
                            "text": f"Search memory for information about: {query}",
                        },
                    }
                ],
            }
        
        raise MCPError(
            code=MCPErrorCode.PROMPT_NOT_FOUND.value,
            message=f"Prompt not found: {name}",
        )


# ---------------------------------------------------------------------------
# FastAPI Integration
# ---------------------------------------------------------------------------

def create_mcp_router(server: ObscuraMCPServer) -> Any:
    """
    Create a FastAPI router for MCP endpoints.
    
    Args:
        server: ObscuraMCPServer instance
        
    Returns:
        FastAPI router with MCP endpoints
    """
    from fastapi import APIRouter, Request
    from sse_starlette.sse import EventSourceResponse
    
    router = APIRouter(prefix="/mcp", tags=["MCP"])
    
    @router.post("/rpc")
    async def handle_rpc(request: Request):  # pyright: ignore[reportUnusedFunction]
        """Handle MCP JSON-RPC requests."""
        body = await request.json()
        
        method = body.get("method")
        params = body.get("params", {})
        req_id = body.get("id")
        
        try:
            if method == "initialize":
                result = await server.handle_initialize(**params)
            elif method == "tools/list":
                result = await server.handle_tools_list()
            elif method == "tools/call":
                context = ObscuraMCPToolContext(user_id="api")
                result_obj = await server.handle_tools_call(
                    name=params["name"],
                    arguments=params.get("arguments", {}),
                    context=context,
                )
                result = {
                    "content": result_obj.content,
                    "isError": result_obj.isError,
                }
            elif method == "resources/list":
                result = await server.handle_resources_list()
            elif method == "resources/read":
                result = await server.handle_resources_read(params["uri"])
            elif method == "prompts/list":
                result = await server.handle_prompts_list()
            elif method == "prompts/get":
                result = await server.handle_prompts_get(
                    name=params["name"],
                    arguments=params.get("arguments"),
                )
            else:
                raise MCPError(
                    code=MCPErrorCode.METHOD_NOT_FOUND.value,
                    message=f"Method not found: {method}",
                )
            
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": result,
            }
        except MCPError as e:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {
                    "code": e.code,
                    "message": e.message,
                    "data": e.data,
                },
            }
        except Exception as e:
            logger.exception("MCP RPC error")
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {
                    "code": MCPErrorCode.INTERNAL_ERROR.value,
                    "message": str(e),
                },
            }
    
    @router.get("/sse")
    async def handle_sse(request: Request):  # pyright: ignore[reportUnusedFunction]
        """Handle MCP SSE (Server-Sent Events) connections."""
        async def event_generator():
            # Send initial endpoint event
            yield {
                "event": "endpoint",
                "data": json.dumps({"uri": "/mcp/rpc"}),
            }
            
            # Keep connection alive
            while True:
                await asyncio.sleep(30)
                yield {
                    "event": "ping",
                    "data": "",
                }
        
        return EventSourceResponse(event_generator())
    
    return router
