"""
sdk.backends — Backend implementations for Obscura.

This package contains various backend implementations:
- mcp_backend: MCP-based tool backend

Additional backends can be added here for different LLM providers.
"""

from sdk.backends.mcp_backend import MCPBackend, MCPBackendMixin

__all__ = [
    "MCPBackend",
    "MCPBackendMixin",
]
