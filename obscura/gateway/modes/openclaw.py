"""OpenClaw mode - delegates to OpenClaw for system access."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from obscura.gateway.config import GatewayConfig
from obscura.gateway.modes.base import BaseMode
from obscura.openclaw_bridge import OpenClawBridge, OpenClawBridgeConfig

logger = logging.getLogger(__name__)


class OpenClawMode(BaseMode):
    """Run as OpenClaw agent with tool delegation."""

    def __init__(self, config: GatewayConfig) -> None:
        super().__init__(config)
        self.bridge: OpenClawBridge | None = None

    @classmethod
    async def is_available(cls, config: GatewayConfig) -> bool:
        """Check if OpenClaw is available."""
        if not config.openclaw.enabled:
            return False

        socket_path = config.openclaw.socket_path
        if socket_path and socket_path.exists():
            return True

        # Try HTTP health check
        try:
            import httpx

            response = httpx.get(f"{config.openclaw.gateway_url}/health", timeout=2.0)
            return response.status_code == 200
        except Exception:
            return False

    async def start(self) -> None:
        """Connect to OpenClaw."""
        bridge_config = OpenClawBridgeConfig(
            base_url=self.config.openclaw.gateway_url,
        )
        self.bridge = OpenClawBridge(config=bridge_config)
        logger.info("OpenClaw mode started")

    async def stop(self) -> None:
        """Disconnect from OpenClaw."""
        if self.bridge:
            await self.bridge.close()
            self.bridge = None
        logger.info("OpenClaw mode stopped")

    async def execute_tool(self, tool_name: str, *args: Any, **kwargs: Any) -> Any:
        """Execute tool via OpenClaw."""
        if not self.bridge:
            raise RuntimeError("OpenClaw bridge not connected")

        # Map tool names to OpenClaw bridge methods
        if tool_name == "spawn_agent":
            return await self.bridge.spawn_agent(
                agent_name=kwargs.get("agent_name", "assistant"),
                prompt=args[0] if args else kwargs.get("prompt", ""),
            )
        elif tool_name == "exec":
            return await self.bridge.run_in_terminal(
                command=args[0] if args else kwargs.get("command", ""),
            )
        else:
            raise ValueError(f"Unknown tool: {tool_name}")

    async def get_status(self) -> dict[str, Any]:
        """Get OpenClaw connection status."""
        return {
            "connected": self.bridge is not None,
            "url": self.config.openclaw.gateway_url,
        }
