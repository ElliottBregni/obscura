"""MCP Bridge mode - Model Context Protocol server."""

from __future__ import annotations

import logging
from typing import Any

from obscura.gateway.config import GatewayConfig
from obscura.gateway.modes.base import BaseMode

logger = logging.getLogger(__name__)


class MCPMode(BaseMode):
    """MCP Bridge mode for tool interoperability."""

    def __init__(self, config: GatewayConfig) -> None:
        super().__init__(config)
        self.running = False

    @classmethod
    async def is_available(cls, config: GatewayConfig) -> bool:
        """MCP mode is available if enabled."""
        return config.mcp.enabled

    async def start(self) -> None:
        """Start MCP server."""
        self.running = True
        logger.info(f"MCP mode started on port {self.config.mcp.port}")

    async def stop(self) -> None:
        """Stop MCP server."""
        self.running = False
        logger.info("MCP mode stopped")

    async def execute_tool(self, tool_name: str, *args: Any, **kwargs: Any) -> Any:
        """Execute tool via MCP."""
        if not self.running:
            raise RuntimeError("MCP mode not running")

        # Would delegate to MCP server
        return {
            "response": f"MCP tool {tool_name} executed",
            "args": args,
            "kwargs": kwargs,
        }

    async def get_status(self) -> dict[str, Any]:
        """Get MCP mode status."""
        return {
            "running": self.running,
            "port": self.config.mcp.port,
            "transport": self.config.mcp.transport,
        }
