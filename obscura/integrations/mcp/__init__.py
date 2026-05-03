"""obscura.mcp — Model Context Protocol integration for Obscura.

This module provides MCP server and client functionality:
- Server: Expose Obscura as an MCP server
- Client: Connect to external MCP servers
- Types: MCP protocol types
- Tools: Tool conversion between MCP and Obscura

Usage::

    # As MCP Server
    from obscura.integrations.mcp.server import ObscuraMCPServer

    server = ObscuraMCPServer()
    await server.initialize()

    # As MCP Client
    from obscura.integrations.mcp.client import MCPClient
    from obscura.integrations.mcp.types import MCPConnectionConfig, MCPTransportType

    config = MCPConnectionConfig(
        transport=MCPTransportType.STDIO,
        command="npx",
        args=["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
    )

    async with MCPClient(config) as client:
        tools = await client.list_tools()
        result = await client.call_tool("read_file", {"path": "/tmp/test.txt"})

Layer note
----------
``obscura.integrations.mcp.server`` is heavyweight: it imports
``obscura.agent.agents``, which transitively imports ``obscura.core.client``.
Eagerly re-exporting it from this ``__init__`` made every consumer of
the leaf modules (``client``, ``types``, ``tools``, ``discovery``,
``config_loader``) pay the cost — and worse, created a partial-init
cycle when ``agent.agents`` itself wanted to import ``mcp.config_loader``
or ``mcp.types``.

Resolution: leaf modules are re-exported eagerly. Server-layer symbols
(``ObscuraMCPServer``, ``create_mcp_router``) are loaded lazily via
``__getattr__`` only when accessed. Callers that want them by name
(``from obscura.integrations.mcp import ObscuraMCPServer``) still work;
they just trigger the heavy import at access time, not at package import.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from obscura.integrations.mcp.catalog import (
    MCPCatalogEntry,
    MCPCatalogProvider,
    MCPRegistryAPICatalogProvider,
    MCPServersOrgCatalogProvider,
    catalog_entries_to_mcp_servers,
    write_catalog_config,
)
from obscura.integrations.mcp.client import MCPClient, MCPSessionManager
from obscura.integrations.mcp.config_loader import (
    DiscoveredMCPServer,
    build_runtime_server_configs,
    discover_mcp_servers,
)
from obscura.integrations.mcp.tools import (
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
from obscura.integrations.mcp.types import (
    MCPCapabilities,
    MCPClientCapabilities,
    MCPConnectionConfig,
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
    ObscuraMCPConfig,
    ObscuraMCPToolContext,
)

if TYPE_CHECKING:
    # Type-checkers see these names so static `from obscura.integrations.mcp
    # import ObscuraMCPServer` resolves correctly. At runtime the same
    # access is served by ``__getattr__`` below, which only loads server.py
    # on demand to avoid the agent.agents cycle.
    from obscura.integrations.mcp.server import (
        ObscuraMCPServer,
        create_mcp_router,
    )

# Names that live in obscura.integrations.mcp.server. Loading server.py
# pulls in obscura.agent.agents → obscura.core.client, so we keep it
# behind ``__getattr__`` to break the cycle.
_LAZY_FROM_SERVER = frozenset({"ObscuraMCPServer", "create_mcp_router"})


def __getattr__(name: str) -> Any:
    if name in _LAZY_FROM_SERVER:
        from obscura.integrations.mcp import server as _server

        return getattr(_server, name)
    raise AttributeError(f"module 'obscura.integrations.mcp' has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(globals()) | _LAZY_FROM_SERVER)


__all__ = [
    # Config loader
    "DiscoveredMCPServer",
    "MCPCapabilities",
    "MCPCatalogEntry",
    "MCPCatalogProvider",
    # Client
    "MCPClient",
    # Types
    "MCPClientCapabilities",
    "MCPConnectionConfig",
    "MCPError",
    "MCPErrorCode",
    "MCPImplementation",
    "MCPPrompt",
    "MCPPromptMessage",
    "MCPPromptResult",
    "MCPRegistryAPICatalogProvider",
    "MCPResource",
    "MCPResourceContent",
    "MCPServersOrgCatalogProvider",
    "MCPSessionManager",
    "MCPTool",
    "MCPToolCall",
    "MCPToolResult",
    "MCPTransportType",
    "ObscuraMCPConfig",
    # Server (lazy)
    "ObscuraMCPServer",
    "ObscuraMCPToolContext",
    # Tools
    "ObscuraMCPToolRegistry",
    "build_runtime_server_configs",
    "catalog_entries_to_mcp_servers",
    "create_array_property",
    "create_boolean_property",
    "create_integer_property",
    "create_mcp_router",
    "create_object_property",
    "create_string_property",
    "discover_mcp_servers",
    "get_obscura_mcp_registry",
    "mcp_result_to_obscura",
    "mcp_tool_to_obscura",
    "obscura_result_to_mcp",
    "obscura_tool_to_mcp",
    "write_catalog_config",
]
