"""
sdk.mcp.types — MCP (Model Context Protocol) type definitions.

Implements the MCP protocol types for communication between MCP clients and servers.
Based on the Model Context Protocol specification.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal, override


# ---------------------------------------------------------------------------
# JSON-RPC Types
# ---------------------------------------------------------------------------


class JSONRPCVersion(Enum):
    """JSON-RPC version constants."""

    V2_0 = "2.0"


@dataclass(frozen=True)
class JSONRPCRequest:
    """JSON-RPC 2.0 request."""

    jsonrpc: str = "2.0"
    id: str | int | None = None
    method: str = ""
    params: dict[str, Any] = field(default_factory=lambda: {})


@dataclass(frozen=True)
class JSONRPCResponse:
    """JSON-RPC 2.0 response."""

    jsonrpc: str = "2.0"
    id: str | int | None = None
    result: Any = None
    error: dict[str, Any] | None = None


@dataclass(frozen=True)
class JSONRPCNotification:
    """JSON-RPC 2.0 notification (request with no id)."""

    jsonrpc: str = "2.0"
    method: str = ""
    params: dict[str, Any] = field(default_factory=lambda: {})


@dataclass(frozen=True)
class JSONRPCError:
    """JSON-RPC 2.0 error object."""

    code: int
    message: str
    data: Any = None


# ---------------------------------------------------------------------------
# MCP Protocol Types
# ---------------------------------------------------------------------------


class MCPMethod(Enum):
    """MCP protocol methods."""

    # Lifecycle
    INITIALIZE = "initialize"
    INITIALIZED = "notifications/initialized"
    PING = "ping"

    # Tools
    TOOLS_LIST = "tools/list"
    TOOLS_CALL = "tools/call"

    # Resources
    RESOURCES_LIST = "resources/list"
    RESOURCES_READ = "resources/read"
    RESOURCES_SUBSCRIBE = "resources/subscribe"
    RESOURCES_UNSUBSCRIBE = "resources/unsubscribe"

    # Prompts
    PROMPTS_LIST = "prompts/list"
    PROMPTS_GET = "prompts/get"

    # Roots (client -> server)
    ROOTS_LIST = "roots/list"

    # Sampling (server -> client)
    SAMPLING_CREATE_MESSAGE = "sampling/createMessage"


@dataclass(frozen=True)
class MCPImplementation:
    """MCP implementation metadata."""

    name: str
    version: str


@dataclass(frozen=True)
class MCPCapabilities:
    """MCP server/client capabilities."""

    experimental: dict[str, Any] = field(default_factory=lambda: {})
    prompts: dict[str, Any] | None = None
    resources: dict[str, Any] | None = None
    tools: dict[str, Any] | None = None
    logging: dict[str, Any] | None = None


@dataclass(frozen=True)
class MCPClientCapabilities:
    """MCP client capabilities."""

    experimental: dict[str, Any] = field(default_factory=lambda: {})
    roots: dict[str, Any] | None = None
    sampling: dict[str, Any] | None = None


@dataclass
class MCPTool:
    """MCP tool definition."""

    name: str
    description: str
    inputSchema: dict[str, Any] = field(default_factory=lambda: {})


@dataclass
class MCPToolCall:
    """MCP tool call."""

    name: str
    arguments: dict[str, Any] = field(default_factory=lambda: {})


@dataclass
class MCPToolResult:
    """MCP tool result."""

    content: list[dict[str, Any]] = field(default_factory=lambda: [])
    isError: bool = False


@dataclass
class MCPResource:
    """MCP resource definition."""

    uri: str
    name: str
    description: str | None = None
    mimeType: str | None = None


@dataclass
class MCPResourceContent:
    """MCP resource content."""

    uri: str
    mimeType: str | None = None
    text: str | None = None
    blob: bytes | None = None


@dataclass
class MCPPrompt:
    """MCP prompt definition."""

    name: str
    description: str | None = None
    arguments: list[dict[str, Any]] | None = None


@dataclass
class MCPPromptMessage:
    """MCP prompt message."""

    role: Literal["user", "assistant"]
    content: dict[str, Any]


@dataclass
class MCPPromptResult:
    """MCP prompt result."""

    description: str | None = None
    messages: list[MCPPromptMessage] = field(default_factory=lambda: [])


@dataclass
class MCPLoggingMessage:
    """MCP logging message."""

    level: Literal[
        "debug", "info", "notice", "warning", "error", "critical", "alert", "emergency"
    ]
    logger: str | None = None
    data: Any = None


# ---------------------------------------------------------------------------
# Connection Types
# ---------------------------------------------------------------------------


class MCPTransportType(Enum):
    """MCP transport types."""

    STDIO = "stdio"
    SSE = "sse"
    WEBSOCKET = "websocket"


@dataclass
class MCPConnectionConfig:
    """Configuration for MCP connection."""

    transport: MCPTransportType
    command: str | None = None  # For stdio transport
    args: list[str] = field(default_factory=lambda: [])  # For stdio transport
    url: str | None = None  # For SSE/WebSocket transport
    env: dict[str, str] = field(default_factory=lambda: {})
    timeout: float = 30.0


@dataclass
class MCPServerInfo:
    """Information about an MCP server."""

    name: str
    version: str
    capabilities: MCPCapabilities
    transport: MCPTransportType


# ---------------------------------------------------------------------------
# Error Codes (MCP-specific)
# ---------------------------------------------------------------------------


class MCPErrorCode(Enum):
    """MCP protocol error codes."""

    PARSE_ERROR = -32700
    INVALID_REQUEST = -32600
    METHOD_NOT_FOUND = -32601
    INVALID_PARAMS = -32602
    INTERNAL_ERROR = -32603

    # MCP-specific errors
    RESOURCE_NOT_FOUND = -32000
    RESOURCE_ACCESS_DENIED = -32001
    PROMPT_NOT_FOUND = -32002
    TOOL_NOT_FOUND = -32003
    TOOL_EXECUTION_ERROR = -32004


@dataclass
class MCPError(Exception):
    """MCP protocol error."""

    code: int
    message: str
    data: Any = None

    @override
    def __str__(self) -> str:
        return f"MCP Error {self.code}: {self.message}"


# ---------------------------------------------------------------------------
# Obscura-specific MCP Types
# ---------------------------------------------------------------------------


@dataclass
class ObscuraMCPToolContext:
    """Context for Obscura MCP tools."""

    user_id: str
    agent_id: str | None = None
    session_id: str | None = None
    request_id: str | None = None


@dataclass
class ObscuraMCPConfig:
    """Configuration for Obscura MCP integration."""

    enabled: bool = True
    servers: list[MCPConnectionConfig] = field(default_factory=lambda: [])
    expose_obscura_as_mcp: bool = True
    allow_external_mcp: bool = True
    tool_timeout: float = 60.0
    max_tools: int = 100
