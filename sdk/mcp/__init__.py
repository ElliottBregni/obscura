"""
sdk.mcp — Model Context Protocol integration for Obscura.

This module provides MCP server and client functionality:
- Server: Expose Obscura as an MCP server
- Client: Connect to external MCP servers
- Types: MCP protocol types
- Tools: Tool conversion between MCP and Obscura

Usage::

    # As MCP Server
    from sdk.mcp.server import ObscuraMCPServer

    server = ObscuraMCPServer()
    await server.initialize()

    # As MCP Client
    from sdk.mcp.client import MCPClient
    from sdk.mcp.types import MCPConnectionConfig, MCPTransportType

    config = MCPConnectionConfig(
        transport=MCPTransportType.STDIO,
        command="npx",
        args=["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
    )

    async with MCPClient(config) as client:
        tools = await client.list_tools()
        result = await client.call_tool("read_file", {"path": "/tmp/test.txt"})
"""

from sdk.mcp.types import (
    MCPClientCapabilities,
    MCPConnectionConfig,
    MCPCapabilities,
    MCPError,
    MCPErrorCode,
    MCPImplementation,
    MCPPrompt,
    MCPPromptMessage,
    MCPPromptResult,
    MCPResource,
    MCPResourceContent,
    MCPTool,
    MCPToolCall,
    MCPToolResult,
    MCPTransportType,
    ObscuraMCPToolContext,
    ObscuraMCPConfig,
)

from sdk.mcp.client import MCPClient, MCPSessionManager

from sdk.mcp.server import ObscuraMCPServer, create_mcp_router

from sdk.mcp.config_loader import DiscoveredMCPServer, build_runtime_server_configs, discover_mcp_servers
from sdk.mcp.catalog import (
    MCPCatalogEntry,
    MCPCatalogProvider,
    MCPRegistryAPICatalogProvider,
    MCPServersOrgCatalogProvider,
    catalog_entries_to_mcp_servers,
    write_catalog_config,
)

from sdk.mcp.tools import (
    ObscuraMCPToolRegistry,
    create_array_property,
    create_boolean_property,
    create_integer_property,
    create_object_property,
    create_string_property,
    get_obscura_mcp_registry,
    mcp_result_to_obscura,
    mcp_tool_to_obscura,
    obscura_result_to_mcp,
    obscura_tool_to_mcp,
)

__all__ = [
    # Types
    "MCPClientCapabilities",
    "MCPConnectionConfig",
    "MCPCapabilities",
    "MCPError",
    "MCPErrorCode",
    "MCPImplementation",
    "MCPPrompt",
    "MCPPromptMessage",
    "MCPPromptResult",
    "MCPResource",
    "MCPResourceContent",
    "MCPTool",
    "MCPToolCall",
    "MCPToolResult",
    "MCPTransportType",
    "ObscuraMCPToolContext",
    "ObscuraMCPConfig",
    # Client
    "MCPClient",
    "MCPSessionManager",
    # Server
    "ObscuraMCPServer",
    "create_mcp_router",
    # Config loader
    "DiscoveredMCPServer",
    "discover_mcp_servers",
    "build_runtime_server_configs",
    "MCPCatalogEntry",
    "MCPCatalogProvider",
    "MCPRegistryAPICatalogProvider",
    "MCPServersOrgCatalogProvider",
    "catalog_entries_to_mcp_servers",
    "write_catalog_config",
    # Tools
    "ObscuraMCPToolRegistry",
    "create_array_property",
    "create_boolean_property",
    "create_integer_property",
    "create_object_property",
    "create_string_property",
    "get_obscura_mcp_registry",
    "mcp_result_to_obscura",
    "mcp_tool_to_obscura",
    "obscura_result_to_mcp",
    "obscura_tool_to_mcp",
]
