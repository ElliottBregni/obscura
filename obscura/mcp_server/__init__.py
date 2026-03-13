"""
obscura.mcp_server -- Standalone FastMCP server for the Obscura API.

Proxies Obscura FastAPI endpoints as MCP tools, enabling connection
from Claude Desktop, Cursor, or any MCP client.

Usage::

    # Run via stdio (for Claude Desktop / Cursor)
    python -m obscura.mcp_server

    # Run as HTTP server
    python -m obscura.mcp_server --transport streamable-http --port 8888

    # With custom Obscura server URL
    OBSCURA_BASE_URL=http://myserver:8080 python -m obscura.mcp_server

Environment variables:
    OBSCURA_BASE_URL   Base URL of the Obscura FastAPI server (default: http://localhost:8080)
    OBSCURA_API_KEY    API key for authenticating with the server (optional)
    OBSCURA_MCP_TIMEOUT  HTTP request timeout in seconds (default: 60)
"""

from obscura.mcp_server.server import mcp

__all__ = ["mcp"]
