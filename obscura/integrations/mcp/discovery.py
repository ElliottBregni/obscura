"""obscura.integrations.mcp.discovery — Probe external MCP servers for their tools.

External MCP servers configured under ``mcp_servers`` in obscura's session
config are passed through to Claude SDK directly: the SDK handles their
invocation. But obscura's own ToolRegistry never learned about them, which
meant:

  * The system prompt's tool listing didn't include them.
  * ``tool_search`` couldn't find them.
  * The model had to guess names and frequently hallucinated the wrong
    namespace prefix (``mcp__obscura_tools__*`` instead of
    ``mcp__<server>__*``).

This module fixes that by connecting to each external server at session
build time, listing its tools, and registering *shadow* :class:`ToolSpec`
entries in the registry. The shadows exist only for discovery — Claude
SDK still routes the actual calls through the original ``mcp_servers``
passthrough. The shadow handler returns a clear error if invoked
directly, which would indicate a routing bug.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

from obscura.integrations.mcp.client import MCPClient
from obscura.integrations.mcp.types import MCPConnectionConfig, MCPTransportType

if TYPE_CHECKING:
    from obscura.core.types import ToolSpec

logger = logging.getLogger(__name__)

# Per-server probe timeout. Keep short so a slow / dead MCP server doesn't
# stall session startup.
_DEFAULT_PROBE_TIMEOUT = 3.0


def _shadow_handler_factory(qualified_name: str) -> Any:
    """Build a handler that errors if invoked.

    Shadow specs exist for discovery only — Claude SDK dispatches the real
    calls. If obscura's own tool dispatch ever invokes one, that's a bug.
    """

    async def _shadow_handler(**_kwargs: Any) -> str:
        return json.dumps(
            {
                "ok": False,
                "error": "shadow_tool_invoked",
                "detail": (
                    f"Tool '{qualified_name}' is provided by an external MCP "
                    "server and should be dispatched by the SDK, not by "
                    "obscura's tool runner. This usually means the routing "
                    "is broken."
                ),
            },
        )

    return _shadow_handler


def _build_config(server: dict[str, Any]) -> MCPConnectionConfig | None:
    """Translate an obscura mcp_servers entry into an MCPConnectionConfig.

    Returns None if the entry is malformed (e.g. missing command for stdio).
    """
    transport_str = str(server.get("transport", "stdio")).lower()
    name = str(server.get("name") or "")

    if transport_str == "stdio":
        command = server.get("command")
        if not command:
            return None
        return MCPConnectionConfig(
            transport=MCPTransportType.STDIO,
            command=str(command),
            args=list(server.get("args") or []),
            env=dict(server.get("env") or {}),
            timeout=_DEFAULT_PROBE_TIMEOUT,
            name=name,
        )

    if transport_str in ("sse", "http"):
        url = server.get("url")
        if not url:
            return None
        # SSE & HTTP both go through SSETransport in obscura's MCP client.
        return MCPConnectionConfig(
            transport=MCPTransportType.SSE,
            url=str(url),
            env=dict(server.get("env") or {}),
            headers=dict(server.get("headers") or {}),
            timeout=_DEFAULT_PROBE_TIMEOUT,
            name=name,
        )

    return None


async def _probe_one_server(
    server: dict[str, Any],
    *,
    timeout: float,
) -> list[ToolSpec]:
    """Connect to one MCP server, list tools, return shadow ToolSpecs.

    Returns an empty list on any failure (logged at INFO level — discovery
    is best-effort and must not break session startup).
    """
    from obscura.core.types import ToolSpec  # local to avoid import cycle

    name = str(server.get("name") or "unknown")
    config = _build_config(server)
    if config is None:
        logger.info("Skipping malformed MCP server config: %s", name)
        return []

    try:
        async with asyncio.timeout(timeout + 1.0):
            async with MCPClient(config) as client:
                tools = await client.list_tools()
    except (TimeoutError, Exception) as exc:
        logger.info("MCP discovery failed for %s: %s", name, exc)
        return []

    specs: list[ToolSpec] = []
    for tool in tools:
        qualified = f"mcp__{name}__{tool.name}"
        spec = ToolSpec(
            name=qualified,
            description=(tool.description or "").strip(),
            parameters=dict(tool.inputSchema or {"type": "object", "properties": {}}),
            handler=_shadow_handler_factory(qualified),
        )
        specs.append(spec)
    logger.info("MCP discovery: %s contributed %d tools", name, len(specs))
    return specs


async def register_external_mcp_tools(
    backend: Any,
    mcp_servers: list[dict[str, Any]],
    *,
    timeout: float = _DEFAULT_PROBE_TIMEOUT,
) -> int:
    """Discover *mcp_servers* and register the shadow specs into *backend*.

    *backend* must expose a ``register_tool(spec)`` method (every obscura
    provider does). Wraps the discovery + registration loop so each
    backend's ``start()`` becomes one line:

        await register_external_mcp_tools(self, self._mcp_servers)

    Returns the number of shadow specs registered. Failures are swallowed
    — discovery is best-effort.
    """
    if not mcp_servers:
        return 0
    try:
        specs = await discover_mcp_tools(mcp_servers, timeout=timeout)
    except Exception as exc:
        logger.info("MCP discovery aborted: %s", exc)
        return 0

    register = getattr(backend, "register_tool", None)
    if register is None:
        return 0
    for spec in specs:
        try:
            register(spec)
        except Exception as exc:
            logger.info("Failed to register shadow spec %s: %s", spec.name, exc)
    return len(specs)


async def discover_mcp_tools(
    mcp_servers: list[dict[str, Any]],
    *,
    timeout: float = _DEFAULT_PROBE_TIMEOUT,
) -> list[ToolSpec]:
    """Probe every server in *mcp_servers* and return shadow ToolSpecs.

    Discovery runs all servers concurrently. Each server has its own
    timeout, so one slow server doesn't block the others. Failures are
    logged but never raised — discovery is purely best-effort.
    """
    if not mcp_servers:
        return []

    results = await asyncio.gather(
        *(_probe_one_server(s, timeout=timeout) for s in mcp_servers),
        return_exceptions=True,
    )

    specs: list[ToolSpec] = []
    for result in results:
        if isinstance(result, list):
            specs.extend(result)
        else:
            logger.info("MCP discovery task raised: %s", result)
    return specs


def discover_mcp_tools_sync(
    mcp_servers: list[dict[str, Any]],
    *,
    timeout: float = _DEFAULT_PROBE_TIMEOUT,
) -> list[ToolSpec]:
    """Synchronous wrapper around :func:`discover_mcp_tools`.

    Convenient for backend ``start()`` paths that aren't async. Spins up a
    fresh event loop if one isn't running; uses asyncio.run otherwise.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is None:
        return asyncio.run(discover_mcp_tools(mcp_servers, timeout=timeout))

    # Inside an existing event loop — run in a worker thread so we don't
    # nest loops.
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            asyncio.run, discover_mcp_tools(mcp_servers, timeout=timeout)
        )
        return future.result(timeout=timeout * len(mcp_servers) + 5.0)
