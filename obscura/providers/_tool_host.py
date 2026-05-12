"""obscura.providers._tool_host — Shared tool-registration mixin for backends.

Every concrete backend (Claude, Copilot, Codex, OpenAI, LocalLLM, ...) keeps:

* an ordered list of registered :class:`ToolSpec` instances, used to build
  the system-prompt tool listing and the SDK-side MCP server, and
* a :class:`ToolRegistry`, used by the agent loop to look up handlers
  (with alias resolution, name sanitization, etc.).

The duplicate-by-name guard and the dual-add into both stores were
copy-pasted across all five backends. This mixin owns the data structures
and the registration method so concrete backends only need a single
``_init_tool_host()`` call in their ``__init__`` to opt in.

Concrete backend ``__init__`` looks like:

    class FooBackend(BackendToolHostMixin):
        def __init__(self, ...) -> None:
            self._init_tool_host()
            ...
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from obscura.core.config import ObscuraConfig
from obscura.core.tools import ToolRegistry
from obscura.integrations.a2a.token_manager import A2ATokenManager

if TYPE_CHECKING:
    from obscura.core.types import ToolSpec
    from obscura.integrations.a2a.openclaw_bridge import OpenClawBridge
    from obscura.integrations.mcp.discovery import DiscoveryReport

logger = logging.getLogger(__name__)


class BackendToolHostMixin:
    """Mixin: own ``_tools`` and ``_tool_registry`` plus ``register_tool``.

    Concrete backends MUST call :meth:`_init_tool_host` in their
    ``__init__`` before registering any tools. The two attributes
    (``_tools`` / ``_tool_registry``) are intentionally exposed directly
    so call sites that read them (system-prompt builders, etc.) keep
    working unchanged after migration.

    ``last_mcp_discovery_report`` is set by
    :func:`obscura.integrations.mcp.discovery.register_external_mcp_tools`
    after each probe, so callers (and the ``mcp_discovery_status`` system
    tool) can inspect per-server outcomes without scraping log lines.
    """

    _tools: list[ToolSpec]
    _tool_registry: ToolRegistry
    last_mcp_discovery_report: DiscoveryReport | None
    _openclaw_bridge: OpenClawBridge | None

    def _init_tool_host(self) -> None:
        """Initialize the empty tool list and registry. Idempotent."""
        if not hasattr(self, "_tools"):
            self._tools = []
        if not hasattr(self, "_tool_registry"):
            self._tool_registry = ToolRegistry()
        if not hasattr(self, "last_mcp_discovery_report"):
            self.last_mcp_discovery_report = None
        if not hasattr(self, "_openclaw_bridge"):
            self._openclaw_bridge = None

    def register_tool(self, spec: ToolSpec) -> None:
        """Register *spec* for use in sessions, skipping duplicates by name.

        The same spec is added to the ordered list (used for system-prompt
        building) and to the alias-aware :class:`ToolRegistry` (used for
        agent-loop dispatch).
        """
        if any(t.name == spec.name for t in self._tools):
            return
        self._tools.append(spec)
        self._tool_registry.register(spec)

    def get_tool_registry(self) -> ToolRegistry:
        """Return the registry. Convenience for callers using the older API."""
        return self._tool_registry

    @property
    def tool_specs(self) -> tuple[ToolSpec, ...]:
        """Immutable snapshot of registered specs in registration order."""
        return tuple(self._tools)

    # -- OpenClaw bridge -----------------------------------------------------

    def _read_openclaw_token(self) -> str | None:
        """Return the OpenClaw gateway token, or ``None`` if not configured.

        Delegates to :class:`~obscura.integrations.a2a.token_manager.A2ATokenManager`
        which checks ``OPENCLAW_TOKEN`` env var first, then falls back to
        ``~/.openclaw/openclaw.json`` → ``gateway.auth.token``.
        """
        return A2ATokenManager().load_openclaw_token()

    async def _init_openclaw_bridge(self) -> None:
        """Connect to the OpenClaw gateway and register the ``ask_openclaw`` tool.

        Reads the token from ``OPENCLAW_TOKEN`` env var or
        ``~/.openclaw/openclaw.json``.  Non-fatal — if no token is found or the
        health check fails, the bridge is set to ``None`` and the tool is not
        registered.

        Skipped entirely when ``OscuraConfig.load().a2a_bridge_enabled`` is
        ``False`` (env ``OBSCURA_A2A_BRIDGE_ENABLED=false``).
        """
        from obscura.core.types import ToolSpec
        from obscura.integrations.a2a.openclaw_bridge import OpenClawBridge

        _cfg = ObscuraConfig.load()
        if not _cfg.a2a_bridge_enabled:
            logger.debug(
                "OpenClaw bridge disabled via config (a2a_bridge_enabled=false) — skipping"
            )
            return

        token = self._read_openclaw_token()
        if not token:
            logger.debug(
                "OPENCLAW_TOKEN not set and ~/.openclaw/openclaw.json not found — skipping OpenClaw bridge"
            )
            return

        bridge = OpenClawBridge.from_config(
            token=token,
            gateway_url=_cfg.a2a_bridge_gateway_url,
        )
        await bridge.connect()
        healthy = await bridge.health_check()
        if healthy:
            logger.debug("OpenClaw bridge connected and healthy")
        else:
            logger.debug(
                "OpenClaw bridge connected but health check failed — gateway may be offline"
            )

        self._openclaw_bridge = bridge

        # Register the ask_openclaw tool now that the bridge is available.
        async def _ask_openclaw_handler(message: str = "") -> str:
            if self._openclaw_bridge is None:
                return json.dumps(
                    {
                        "error": "openclaw_bridge_unavailable",
                        "detail": "OpenClaw bridge is not connected",
                    }
                )
            task = await self._openclaw_bridge.send(message)
            from obscura.core.enums.protocol import A2ATaskState
            from obscura.core.models.a2a import TextPart

            if task.status.state == A2ATaskState.COMPLETED and task.artifacts:
                parts = task.artifacts[0].parts
                return "".join(p.text for p in parts if isinstance(p, TextPart))
            # Failed task — return the status message text
            msg = task.status.message
            if msg and msg.parts:
                from obscura.core.models.a2a import TextPart as _TP

                return json.dumps(
                    {
                        "error": "openclaw_failed",
                        "detail": "".join(
                            p.text for p in msg.parts if isinstance(p, _TP)
                        ),
                    }
                )
            return json.dumps({"error": "openclaw_failed", "detail": "Unknown error"})

        spec = ToolSpec(
            name="ask_openclaw",
            description="Send a message to Molty (OpenClaw/Kimi K2.5 agent) and get a response",
            parameters={
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "description": "The message to send to Molty",
                    }
                },
                "required": ["message"],
            },
            handler=_ask_openclaw_handler,
        )
        self.register_tool(spec)
