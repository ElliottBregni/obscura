"""Native mode - direct system access without OpenClaw."""

from __future__ import annotations

import logging
import subprocess
from typing import TYPE_CHECKING, Any

from obscura.gateway.config import GatewayConfig
from obscura.gateway.modes.base import BaseMode

if TYPE_CHECKING:
    from obscura.integrations.messaging.runners import ObscuraAgentRunner

logger = logging.getLogger(__name__)


class NativeMode(BaseMode):
    """Standalone mode with direct system access."""

    def __init__(self, config: GatewayConfig) -> None:
        super().__init__(config)
        self.running = False
        self._runner: ObscuraAgentRunner | None = None

    @classmethod
    async def is_available(cls, config: GatewayConfig) -> bool:
        """Native mode is always available."""
        return config.native.enabled

    async def start(self) -> None:
        """Start native mode."""
        self.running = True
        logger.info(
            f"Native mode started on {self.config.native.host}:{self.config.native.port}"
        )

        # Lazily build the agent runner — deferred so NativeMode has no hard
        # dep on the full provider stack at import time.
        try:
            import os  # noqa: PLC0415

            from obscura.composition.core import build_core_session  # noqa: PLC0415
            from obscura.composition.session import SessionConfig  # noqa: PLC0415
            from obscura.integrations.messaging.runners import ObscuraAgentRunner  # noqa: PLC0415

            backend_name = os.environ.get("OBSCURA_BACKEND", "copilot")
            session = await build_core_session(
                SessionConfig(
                    backend=backend_name,
                    inject_claude_context=False,
                ),
                surface="api",
            )
            self._runner = ObscuraAgentRunner(
                backend=session.backend,
                tool_registry=session.registry,
            )
            logger.info(
                "NativeMode agent runner initialised (backend=%s)", backend_name
            )
        except ImportError:
            logger.warning(
                "Agent runner deps unavailable — spawn_agent will return an error; "
                "exec and file_read are unaffected",
                exc_info=True,
            )
        except Exception:
            logger.warning(
                "Agent runner failed to initialise — spawn_agent will return an error",
                exc_info=True,
            )

    async def stop(self) -> None:
        """Stop native mode."""
        self.running = False
        self._runner = None
        logger.info("Native mode stopped")

    async def execute_tool(self, tool_name: str, *args: Any, **kwargs: Any) -> Any:
        """Execute tool directly."""
        if not self.running:
            raise RuntimeError("Native mode not running")

        if tool_name == "spawn_agent":
            prompt: str = kwargs.get("prompt", args[0] if args else "")
            context: list[dict[str, str]] = kwargs.get("context", [])
            session_id: str = kwargs.get("session_id") or "native-session"
            system_prompt: str = kwargs.get(
                "system_prompt", "You are a helpful assistant."
            )
            max_turns: int = kwargs.get("max_turns", 8)

            if self._runner is None:
                return {
                    "error": "agent runner unavailable — check logs for details",
                    "session_id": session_id,
                }

            # Normalise history: handle both "content" and "text" key names.
            normalised_context: list[dict[str, str]] = []
            for entry in context:
                role = entry.get("role", "user")
                text = entry.get("text") or entry.get("content", "")
                normalised_context.append({"role": role, "text": text})

            response = await self._runner.run_turn(
                prompt,
                session_id=session_id,
                history=normalised_context,
                system_prompt=system_prompt,
                max_turns=max_turns,
            )
            return {"response": response, "session_id": session_id}

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
