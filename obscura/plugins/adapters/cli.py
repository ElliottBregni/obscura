"""CLI adapter — wraps external binary tools as Obscura plugin handlers."""

from __future__ import annotations

import asyncio
import logging
import shutil
from typing import Any

from obscura.plugins.models import PluginSpec

logger = logging.getLogger(__name__)


class CLIAdapter:
    """Adapter for CLI-binary plugins (runtime_type == 'cli').

    Creates async handlers that shell out to the binary specified in
    each tool's handler field.  Handler format: ``binary arg1 arg2 {param}``
    where ``{param}`` is substituted from tool call arguments.
    """

    def can_handle(self, spec: PluginSpec) -> bool:
        return spec.runtime_type == "cli"

    async def load(self, spec: PluginSpec, config: dict[str, Any]) -> dict[str, Any]:
        handlers: dict[str, Any] = {}
        for tool in spec.tools:
            if not tool.handler:
                continue
            handlers[tool.name] = _make_cli_handler(tool.handler, tool.name)
            logger.debug("Created CLI handler for %s → %s", tool.name, tool.handler)
        return {"handlers": handlers}

    async def healthcheck(self, spec: PluginSpec) -> bool:
        if spec.healthcheck and spec.healthcheck.type == "binary":
            return shutil.which(spec.healthcheck.target) is not None
        # Check that the first tool's binary exists
        for tool in spec.tools:
            if tool.handler:
                binary = tool.handler.split()[0]
                return shutil.which(binary) is not None
        return True

    async def teardown(self, spec: PluginSpec) -> None:
        pass


def _make_cli_handler(handler_template: str, tool_name: str):
    """Create an async handler that executes a CLI command."""

    async def _handler(**kwargs: Any) -> str:
        cmd = handler_template
        for key, value in kwargs.items():
            cmd = cmd.replace(f"{{{key}}}", str(value))

        logger.debug("CLI tool %s executing: %s", tool_name, cmd)
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        output = stdout.decode() if stdout else ""
        if proc.returncode != 0:
            err = stderr.decode() if stderr else ""
            raise RuntimeError(f"CLI tool {tool_name} failed (rc={proc.returncode}): {err}")
        return output

    _handler.__name__ = f"cli_{tool_name}"
    return _handler
