"""Runtime bridge that lets non-SDK backends actually invoke external MCP tools.

Background
----------

``register_external_mcp_tools`` (see ``obscura/integrations/mcp/discovery.py``)
opens a short-lived connection to each configured MCP server, lists its tools,
and registers shadow :class:`ToolSpec` entries on the backend so the model can
*see* them in the system prompt. The shadow handler is intentionally
error-returning — for SDK-native backends (Claude, Copilot) the actual tool
call is dispatched by the SDK itself via its own ``mcp_servers`` passthrough,
so Obscura's tool runner never touches them.

Backends that do **not** have native MCP support in their SDK (currently
``openai`` and ``localllm``) hit the shadow handler at runtime and the call
fails with ``"shadow_tool_invoked"``. This module fixes that by:

1. Holding a long-lived :class:`MCPSessionManager` with one
   :class:`MCPClient` per configured server.
2. Replacing each shadow spec on the backend with a real handler that
   dispatches to the appropriate session and converts the result into
   the same shape obscura's tool runner expects.

The bridge is best-effort: a server that fails to connect during ``start``
just leaves its shadow specs in place (and the original error response is
what the model will see if it tries to call them) — discovery for the other
servers continues normally.
"""

from __future__ import annotations

import logging
from typing import Any

from obscura.core.types import ToolSpec
from obscura.integrations.mcp.client import MCPSessionManager
from obscura.integrations.mcp.discovery import build_mcp_connection_config
from obscura.integrations.mcp.tools import mcp_result_to_obscura

logger = logging.getLogger(__name__)


class MCPExecutionBridge:
    """Hold persistent MCP sessions and patch shadow handlers into real ones.

    Lifecycle:

    * :meth:`start` opens one persistent session per ``mcp_servers`` entry.
      Failures are logged and skipped — the bridge never raises during start.
    * :meth:`install_handlers` walks the backend's discovery report and, for
      every ``mcp__<server>__<tool>`` shadow spec whose server has an open
      session, substitutes a real handler that dispatches to the session.
    * :meth:`stop` closes every session.

    The bridge is a no-op when ``mcp_servers`` is empty, so backends can
    construct one unconditionally.
    """

    def __init__(self, mcp_servers: list[dict[str, Any]]) -> None:
        self._mcp_servers = list(mcp_servers or [])
        self._manager = MCPSessionManager()
        self._started = False
        self._connected_names: set[str] = set()

    @property
    def started(self) -> bool:
        return self._started

    @property
    def connected_servers(self) -> frozenset[str]:
        """Names of servers with an open session. Useful for tests + logging."""
        return frozenset(self._connected_names)

    async def start(self) -> None:
        """Open one persistent MCP session per server. Best-effort."""
        if self._started:
            return
        self._started = True  # Set first so a partial failure doesn't re-trigger.
        if not self._mcp_servers:
            return

        for server in self._mcp_servers:
            name = str(server.get("name") or "").strip()
            if not name:
                logger.warning("MCP bridge: skipping server with no name")
                continue
            config = build_mcp_connection_config(server)
            if config is None:
                logger.warning(
                    "MCP bridge: skipping %s — malformed config (missing command/url)",
                    name,
                )
                continue
            try:
                await self._manager.add_session(name, config)
                self._connected_names.add(name)
                logger.info("MCP bridge: connected to %s", name)
            except Exception as exc:
                logger.warning("MCP bridge: failed to connect to %s: %s", name, exc)

    async def stop(self) -> None:
        """Close every open session. Safe to call when ``start`` was never called."""
        if not self._started:
            return
        try:
            await self._manager.close_all()
        except Exception as exc:
            logger.warning("MCP bridge: error during close_all: %s", exc)
        finally:
            self._connected_names.clear()
            self._started = False

    def install_handlers(self, backend: Any) -> int:
        """Replace shadow handlers on *backend* with real dispatchers.

        Walks ``backend.last_mcp_discovery_report.specs`` (populated by
        ``register_external_mcp_tools``). For each shadow whose server is
        connected, builds a fresh :class:`ToolSpec` with the same name /
        description / parameters but a real handler closure, and registers
        it on the backend's tool registry (which overwrites by name) and
        in the ordered ``_tools`` list.

        Returns the number of handlers actually installed.
        """
        report = getattr(backend, "last_mcp_discovery_report", None)
        if report is None or not getattr(report, "specs", None):
            return 0
        if not self._connected_names:
            return 0

        registry = getattr(backend, "_tool_registry", None)
        tools_list = getattr(backend, "_tools", None)
        if registry is None or tools_list is None:
            logger.warning(
                "MCP bridge: backend %s missing _tool_registry/_tools — skipping",
                type(backend).__name__,
            )
            return 0

        installed = 0
        for spec in report.specs:
            parsed = _parse_qualified_name(spec.name)
            if parsed is None:
                continue
            server_name, tool_name = parsed
            if server_name not in self._connected_names:
                continue
            new_spec = ToolSpec(
                name=spec.name,
                description=spec.description,
                parameters=spec.parameters,
                handler=self._make_handler(server_name, tool_name),
            )
            registry.register(new_spec)
            for i, existing in enumerate(tools_list):
                if existing.name == spec.name:
                    tools_list[i] = new_spec
                    break
            installed += 1
        logger.info(
            "MCP bridge: installed %d real handler(s) over shadow specs", installed
        )
        return installed

    def _make_handler(self, server_name: str, tool_name: str) -> Any:
        """Build the per-tool dispatcher closure."""

        async def handler(**kwargs: Any) -> Any:
            client = self._manager.get_session(server_name)
            if client is None:
                return {
                    "error": "mcp_session_unavailable",
                    "detail": (
                        f"MCP server '{server_name}' has no open session. "
                        "The bridge may have failed to connect at startup."
                    ),
                }
            try:
                result = await client.call_tool(tool_name, kwargs)
            except Exception as exc:
                logger.warning(
                    "MCP bridge: call_tool(%s.%s) failed: %s",
                    server_name,
                    tool_name,
                    exc,
                )
                return {
                    "error": "mcp_call_failed",
                    "server": server_name,
                    "tool": tool_name,
                    "detail": f"{type(exc).__name__}: {exc}",
                }
            return mcp_result_to_obscura(result)

        return handler


def _parse_qualified_name(name: str) -> tuple[str, str] | None:
    """Split ``mcp__<server>__<tool>`` into ``(server, tool)``.

    Tool names themselves can contain ``__`` (e.g. ``mcp__forge__list_models``
    is fine, but a tool literally named ``foo__bar`` would be too). We split
    on the first two ``__`` boundaries only and let the remainder be the tool
    name.
    """
    if not name.startswith("mcp__"):
        return None
    rest = name[len("mcp__") :]
    server, sep, tool = rest.partition("__")
    if not sep or not server or not tool:
        return None
    return server, tool
