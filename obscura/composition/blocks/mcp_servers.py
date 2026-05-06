"""obscura.composition.blocks.mcp_servers — connect external MCP servers.

Extracted from ``ObscuraClient.start()`` so the composition layer can
own MCP backend lifecycle without going through the client. The block
connects to each configured server, registers their tools onto
``session.registry`` + the underlying backend, and registers the
``MCPBackend`` for LIFO teardown so sockets close on session aclose.

Reads:
    config.mcp_servers   — list of server configs
    config.backend       — Codex skips this block (it has its own MCP
                           handling via the SDK's `-c mcp_servers.X` path)

Writes:
    session.registry     — adds MCP tool specs (via session.add_tool)
    session.client._backend — same specs registered for tool-use prompts

Resources:
    Registers the MCPBackend instance for LIFO teardown.

Opt-out:
    1. config.mcp_servers is empty → return immediately
    2. config.backend == "codex"  → return (Codex SDK owns MCP routing)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from obscura.composition.session import AgentSession, SessionConfig

logger = logging.getLogger(__name__)


async def install_mcp_servers(
    session: AgentSession,
    config: SessionConfig,
) -> None:
    """Connect MCP servers and register their tools with the session."""
    if not config.mcp_servers:
        return

    if (config.backend or "").lower() == "codex":
        # Codex routes MCP via its own SDK config; don't double-bind
        return

    try:
        from obscura.core.enums.protocol import MCPTransport
        from obscura.integrations.mcp.types import MCPConnectionConfig
        from obscura.providers.mcp_backend import MCPBackend

        configs: list[MCPConnectionConfig] = []
        for server in config.mcp_servers:
            transport = MCPTransport(server.get("transport", "stdio"))
            configs.append(
                MCPConnectionConfig(
                    transport=transport,
                    command=server.get("command"),
                    args=server.get("args", []),
                    url=server.get("url"),
                    env=server.get("env", {}),
                    headers=server.get("headers", {}),
                    name=server.get("name", ""),
                ),
            )

        mcp_backend = MCPBackend(configs)
        await mcp_backend.start()
    except Exception:
        logger.exception("install_mcp_servers: connection failed")
        return

    mcp_tools = mcp_backend.list_tools()
    for spec in mcp_tools:
        session.add_tool(spec)

    if not mcp_tools:
        logger.warning(
            "MCP servers configured but no tools registered. Connection errors: %s",
            mcp_backend.connection_errors,
        )

    # Register for LIFO teardown so the socket closes on session aclose
    session.register_resource(mcp_backend, name="mcp_backend")
    logger.info(
        "install_mcp_servers: connected %d server(s), registered %d tool(s)",
        len(configs),
        len(mcp_tools),
    )
