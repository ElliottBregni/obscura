"""Native mode - direct system access without OpenClaw."""

from __future__ import annotations

import logging
import subprocess
from typing import Any

from obscura.gateway.config import GatewayConfig
from obscura.gateway.modes.base import BaseMode

logger = logging.getLogger(__name__)


class NativeMode(BaseMode):
    """Standalone mode with direct system access."""

    def __init__(self, config: GatewayConfig) -> None:
        super().__init__(config)
        self.running = False

    @classmethod
    async def is_available(cls, config: GatewayConfig) -> bool:
        """Native mode is always available."""
        return config.native.enabled

    async def start(self) -> None:
        """Start native mode."""
        self.running = True
        logger.info(f"Native mode started on {self.config.native.host}:{self.config.native.port}")

    async def stop(self) -> None:
        """Stop native mode."""
        self.running = False
        logger.info("Native mode stopped")

    async def execute_tool(self, tool_name: str, *args: Any, **kwargs: Any) -> Any:
        """Execute tool directly."""
        if not self.running:
            raise RuntimeError("Native mode not running")

        if tool_name == "spawn_agent":
            # For now, just echo back - would spawn actual agent
            prompt = args[0] if args else kwargs.get("prompt", "")
            return {
                "response": f"Agent received: {prompt[:50]}...",
                "session_id": "native-session-001",
            }

        elif tool_name == "exec":
            command = args[0] if args else kwargs.get("command", "")
            if not self.config.native.allow_shell_exec:
                raise PermissionError("Shell execution disabled")

            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
            return {
                "stdout": result.stdout,
                "stderr": result.stderr,
                "returncode": result.returncode,
            }

        elif tool_name == "file_read":
            path = args[0] if args else kwargs.get("path", "")
            with open(path) as f:
                return f.read()

        else:
            raise ValueError(f"Unknown tool: {tool_name}")

    async def get_status(self) -> dict[str, Any]:
        """Get native mode status."""
        return {
            "running": self.running,
            "port": self.config.native.port,
            "elevated": self.config.native.elevated,
        }
