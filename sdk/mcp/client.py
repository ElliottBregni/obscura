"""
sdk.mcp.client — MCP client for connecting to external MCP servers.

Supports stdio and SSE transports for connecting to MCP servers.

Usage::

    from sdk.mcp.client import MCPClient
    from sdk.mcp.types import MCPConnectionConfig, MCPTransportType

    # Connect via stdio
    config = MCPConnectionConfig(
        transport=MCPTransportType.STDIO,
        command="npx",
        args=["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
    )

    async with MCPClient(config) as client:
        tools = await client.list_tools()
        result = await client.call_tool("read_file", {"path": "/tmp/test.txt"})
"""

import asyncio
import json
import logging
import subprocess
import types
from typing import Any, override

import httpx

from sdk.mcp.types import (
    MCPConnectionConfig,
    MCPError,
    MCPErrorCode,
    MCPPrompt,
    MCPPromptMessage,
    MCPPromptResult,
    MCPResource,
    MCPResourceContent,
    MCPTool,
    MCPToolResult,
    MCPTransportType,
)

logger = logging.getLogger(__name__)


class MCPClient:
    """
    Client for connecting to MCP servers.

    Supports stdio and SSE transports.
    """

    def __init__(self, config: MCPConnectionConfig) -> None:
        self.config = config
        self._transport: MCPTransport | None = None
        self._initialized = False
        self._request_id = 0
        self._pending_requests: dict[str, asyncio.Future[dict[str, Any]]] = {}

    async def __aenter__(self) -> "MCPClient":
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: types.TracebackType | None,
    ) -> None:
        await self.disconnect()

    async def connect(self) -> None:
        """Connect to the MCP server."""
        if self._initialized:
            return

        if self.config.transport == MCPTransportType.STDIO:
            self._transport = StdioTransport(self.config)
        elif self.config.transport == MCPTransportType.SSE:
            self._transport = SSETransport(self.config)
        else:
            raise MCPError(
                code=MCPErrorCode.INVALID_PARAMS.value,
                message=f"Unsupported transport: {self.config.transport}",
            )

        await self._transport.connect()

        # Send initialize request
        init_response = await self._request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {
                    "name": "obscura-mcp-client",
                    "version": "0.2.0",
                },
            },
        )

        # Send initialized notification
        await self._notification("notifications/initialized", {})

        self._initialized = True
        logger.info(f"Connected to MCP server: {init_response}")

    async def disconnect(self) -> None:
        """Disconnect from the MCP server."""
        if self._transport:
            await self._transport.disconnect()
            self._transport = None

        # Cancel any pending requests
        for future in self._pending_requests.values():
            if not future.done():
                future.cancel()
        self._pending_requests.clear()

        self._initialized = False

    def _next_id(self) -> str:
        """Generate next request ID."""
        self._request_id += 1
        return str(self._request_id)

    async def _request(self, method: str, params: dict[str, Any]) -> Any:
        """Send a JSON-RPC request and wait for response."""
        if self._transport is None:
            raise MCPError(
                code=MCPErrorCode.INTERNAL_ERROR.value,
                message="Not connected to MCP server",
            )

        req_id = self._next_id()
        request: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params,
        }

        future: asyncio.Future[dict[str, Any]] = asyncio.get_event_loop().create_future()
        self._pending_requests[req_id] = future

        try:
            await self._transport.send(request)

            # Wait for response with timeout
            response: dict[str, Any] = await asyncio.wait_for(
                future,
                timeout=self.config.timeout,
            )

            if "error" in response:
                error: dict[str, Any] = response["error"]
                raise MCPError(
                    code=error.get("code", MCPErrorCode.INTERNAL_ERROR.value),
                    message=error.get("message", "Unknown error"),
                    data=error.get("data"),
                )

            return response.get("result")
        finally:
            self._pending_requests.pop(req_id, None)

    async def _notification(self, method: str, params: dict[str, Any]) -> None:
        """Send a JSON-RPC notification (no response expected)."""
        if self._transport is None:
            return

        notification: dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }
        await self._transport.send(notification)

    def _handle_response(self, response: dict[str, Any]) -> None:
        """Handle incoming JSON-RPC response."""
        req_id = str(response.get("id"))
        if req_id in self._pending_requests:
            future = self._pending_requests[req_id]
            if not future.done():
                future.set_result(response)

    # -----------------------------------------------------------------------
    # MCP Protocol Methods
    # -----------------------------------------------------------------------

    async def ping(self) -> dict[str, Any]:
        """Send ping to server."""
        return await self._request("ping", {})

    async def list_tools(self) -> list[MCPTool]:
        """List available tools from the server."""
        result = await self._request("tools/list", {})

        tools: list[MCPTool] = []
        for tool_data in result.get("tools", []):
            tools.append(MCPTool(
                name=tool_data["name"],
                description=tool_data.get("description", ""),
                inputSchema=tool_data.get("inputSchema", {}),
            ))

        return tools

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> MCPToolResult:
        """Call a tool on the server."""
        result = await self._request(
            "tools/call",
            {
                "name": name,
                "arguments": arguments,
            },
        )

        return MCPToolResult(
            content=result.get("content", []),
            isError=result.get("isError", False),
        )

    async def list_resources(self) -> list[MCPResource]:
        """List available resources from the server."""
        result = await self._request("resources/list", {})

        resources: list[MCPResource] = []
        for res_data in result.get("resources", []):
            resources.append(MCPResource(
                uri=res_data["uri"],
                name=res_data["name"],
                description=res_data.get("description"),
                mimeType=res_data.get("mimeType"),
            ))

        return resources

    async def read_resource(self, uri: str) -> MCPResourceContent:
        """Read a resource from the server."""
        result = await self._request("resources/read", {"uri": uri})

        contents = result.get("contents", [])
        if not contents:
            raise MCPError(
                code=MCPErrorCode.RESOURCE_NOT_FOUND.value,
                message=f"Resource empty: {uri}",
            )

        content = contents[0]
        return MCPResourceContent(
            uri=content["uri"],
            mimeType=content.get("mimeType"),
            text=content.get("text"),
            blob=content.get("blob"),
        )

    async def subscribe_resource(self, uri: str) -> None:
        """Subscribe to resource updates."""
        await self._request("resources/subscribe", {"uri": uri})

    async def unsubscribe_resource(self, uri: str) -> None:
        """Unsubscribe from resource updates."""
        await self._request("resources/unsubscribe", {"uri": uri})

    async def list_prompts(self) -> list[MCPPrompt]:
        """List available prompts from the server."""
        result = await self._request("prompts/list", {})

        prompts: list[MCPPrompt] = []
        for prompt_data in result.get("prompts", []):
            prompts.append(MCPPrompt(
                name=prompt_data["name"],
                description=prompt_data.get("description"),
                arguments=prompt_data.get("arguments"),
            ))

        return prompts

    async def get_prompt(
        self,
        name: str,
        arguments: dict[str, str] | None = None,
    ) -> MCPPromptResult:
        """Get a prompt from the server."""
        params: dict[str, Any] = {"name": name}
        if arguments:
            params["arguments"] = arguments

        result = await self._request("prompts/get", params)

        messages: list[MCPPromptMessage] = []
        for msg_data in result.get("messages", []):
            messages.append(MCPPromptMessage(
                role=msg_data["role"],
                content=msg_data["content"],
            ))

        return MCPPromptResult(
            description=result.get("description"),
            messages=messages,
        )


# ---------------------------------------------------------------------------
# Transport Implementations
# ---------------------------------------------------------------------------

class MCPTransport:
    """Base class for MCP transports."""

    async def connect(self) -> None:
        raise NotImplementedError

    async def disconnect(self) -> None:
        raise NotImplementedError

    async def send(self, message: dict[str, Any]) -> None:
        raise NotImplementedError

    async def receive(self) -> dict[str, Any] | None:
        raise NotImplementedError


class StdioTransport(MCPTransport):
    """Stdio transport for MCP (spawns subprocess)."""

    def __init__(self, config: MCPConnectionConfig) -> None:
        self.config = config
        self._process: asyncio.subprocess.Process | None = None
        self._read_task: asyncio.Task[None] | None = None
        self._message_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._client: MCPClient | None = None

    @override
    async def connect(self) -> None:
        """Spawn the MCP server process."""
        if not self.config.command:
            raise MCPError(
                code=MCPErrorCode.INVALID_PARAMS.value,
                message="Command required for stdio transport",
            )

        env = {**self.config.env}

        self._process = await asyncio.create_subprocess_exec(
            self.config.command,
            *self.config.args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )

        # Start reading stdout
        self._read_task = asyncio.create_task(self._read_loop())

        logger.info(f"Started MCP server process: {self.config.command}")

    @override
    async def disconnect(self) -> None:
        """Terminate the MCP server process."""
        if self._read_task:
            self._read_task.cancel()
            try:
                await self._read_task
            except asyncio.CancelledError:
                pass
            self._read_task = None

        if self._process:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                self._process.kill()
                await self._process.wait()
            self._process = None

    @override
    async def send(self, message: dict[str, Any]) -> None:
        """Send a message to the process stdin."""
        if self._process is None or self._process.stdin is None:
            raise MCPError(
                code=MCPErrorCode.INTERNAL_ERROR.value,
                message="Process not running",
            )

        data = json.dumps(message) + "\n"
        self._process.stdin.write(data.encode())
        await self._process.stdin.drain()

    @override
    async def receive(self) -> dict[str, Any] | None:
        """Receive a message from the queue."""
        try:
            return await asyncio.wait_for(
                self._message_queue.get(),
                timeout=1.0,
            )
        except asyncio.TimeoutError:
            return None

    async def _read_loop(self) -> None:
        """Read messages from process stdout."""
        if self._process is None or self._process.stdout is None:
            return

        try:
            while True:
                line: bytes = await self._process.stdout.readline()
                if not line:
                    break

                try:
                    message: dict[str, Any] = json.loads(line.decode().strip())
                    await self._message_queue.put(message)
                except json.JSONDecodeError:
                    logger.warning(f"Invalid JSON from MCP server: {line}")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Error reading from MCP server: {e}")


class SSETransport(MCPTransport):
    """Server-Sent Events transport for MCP."""

    def __init__(self, config: MCPConnectionConfig) -> None:
        self.config = config
        self._client: httpx.AsyncClient | None = None
        self._event_source: Any = None
        self._endpoint: str | None = None
        self._message_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._read_task: asyncio.Task[None] | None = None

    @override
    async def connect(self) -> None:
        """Connect to SSE endpoint."""
        if not self.config.url:
            raise MCPError(
                code=MCPErrorCode.INVALID_PARAMS.value,
                message="URL required for SSE transport",
            )

        self._client = httpx.AsyncClient(timeout=self.config.timeout)

        # Connect to SSE endpoint to get POST endpoint
        sse_url = f"{self.config.url}/sse"

        # Simple SSE connection - just get the initial endpoint
        response = await self._client.get(sse_url)
        response.raise_for_status()

        # For simplicity, assume POST endpoint is at /rpc
        self._endpoint = f"{self.config.url}/rpc"

        logger.info(f"Connected to MCP SSE endpoint: {self.config.url}")

    @override
    async def disconnect(self) -> None:
        """Disconnect from SSE endpoint."""
        if self._read_task:
            self._read_task.cancel()
            try:
                await self._read_task
            except asyncio.CancelledError:
                pass

        if self._client:
            await self._client.aclose()
            self._client = None

    @override
    async def send(self, message: dict[str, Any]) -> None:
        """Send a message via POST."""
        if self._client is None or self._endpoint is None:
            raise MCPError(
                code=MCPErrorCode.INTERNAL_ERROR.value,
                message="Not connected",
            )

        response = await self._client.post(
            self._endpoint,
            json=message,
        )
        response.raise_for_status()

        # For responses to requests with IDs, put in queue
        if message.get("id") is not None:
            result: dict[str, Any] = response.json()
            await self._message_queue.put(result)

    @override
    async def receive(self) -> dict[str, Any] | None:
        """Receive a message from the queue."""
        try:
            return await asyncio.wait_for(
                self._message_queue.get(),
                timeout=1.0,
            )
        except asyncio.TimeoutError:
            return None


# ---------------------------------------------------------------------------
# Session Management
# ---------------------------------------------------------------------------

class MCPSessionManager:
    """Manager for multiple MCP client sessions."""

    def __init__(self) -> None:
        self._sessions: dict[str, MCPClient] = {}

    async def add_session(self, name: str, config: MCPConnectionConfig) -> MCPClient:
        """Add and connect a new session."""
        client = MCPClient(config)
        await client.connect()
        self._sessions[name] = client
        return client

    async def remove_session(self, name: str) -> None:
        """Remove and disconnect a session."""
        if name in self._sessions:
            await self._sessions[name].disconnect()
            del self._sessions[name]

    def get_session(self, name: str) -> MCPClient | None:
        """Get a session by name."""
        return self._sessions.get(name)

    def list_sessions(self) -> list[str]:
        """List all session names."""
        return list(self._sessions.keys())

    async def close_all(self) -> None:
        """Close all sessions."""
        for client in self._sessions.values():
            await client.disconnect()
        self._sessions.clear()

    async def aggregate_tools(self) -> list[MCPTool]:
        """Aggregate tools from all sessions."""
        all_tools: list[MCPTool] = []
        for name, client in self._sessions.items():
            try:
                tools = await client.list_tools()
                for tool in tools:
                    # Prefix tool name with session name to avoid collisions
                    tool.name = f"{name}.{tool.name}"
                all_tools.extend(tools)
            except Exception as e:
                logger.warning(f"Failed to get tools from {name}: {e}")

        return all_tools
