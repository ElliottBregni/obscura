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
    """Connect MCP servers and register their tools with the session.

    Also writes a list of per-server :class:`MCPServerStatus` entries
    onto ``session.mcp_status`` so both the TUI header and bordered
    REPL session card can render connected / failed / unknown badges
    without re-running the connection logic.
    """
    from obscura.integrations.mcp.types import MCPServerStatus

    if not config.mcp_servers:
        session.mcp_status = []
        return

    if (config.backend or "").lower() == "codex":
        # Codex routes MCP via its own SDK config; don't double-bind.
        # Mark configured servers as "unknown" so the UI shows them
        # without claiming they're connected through us.
        session.mcp_status = [
            MCPServerStatus(
                name=str(srv.get("name", "")) or f"mcp-{idx}",
                state="unknown",
                transport=str(srv.get("transport", "stdio")),
            )
            for idx, srv in enumerate(config.mcp_servers)
        ]
        return

    from obscura.core.enums.protocol import MCPTransport
    from obscura.integrations.mcp.types import MCPConnectionConfig
    from obscura.providers.mcp_backend import MCPBackend

    configs: list[MCPConnectionConfig] = []
    raw_names: list[str] = []
    for idx, server in enumerate(config.mcp_servers):
        name = str(server.get("name", "")) or f"mcp-{idx}"
        raw_names.append(name)
        transport = MCPTransport(server.get("transport", "stdio"))
        configs.append(
            MCPConnectionConfig(
                transport=transport,
                command=server.get("command"),
                args=server.get("args", []),
                url=server.get("url"),
                env=server.get("env", {}),
                headers=server.get("headers", {}),
                name=name,
            ),
        )

    # Per-server status defaults to "unknown". Only servers that
    # appear in ``MCPBackend.connection_errors`` get tagged "failed";
    # servers that surfaced ≥1 tool flip to "connected" below. The
    # default-failed approach was wrong because servers can connect
    # cleanly but expose no tools (still healthy) — those should not
    # render as red.
    statuses: dict[str, MCPServerStatus] = {
        cfg.name: MCPServerStatus(
            name=cfg.name,
            state="unknown",
            transport=cfg.transport.value
            if hasattr(cfg.transport, "value")
            else str(cfg.transport),
            error="",
        )
        for cfg in configs
    }

    mcp_backend: MCPBackend | None = None
    try:
        mcp_backend = MCPBackend(configs)
        await mcp_backend.start()
    except Exception as exc:
        logger.exception("install_mcp_servers: connection failed")
        # Whole-backend failure — every server is failed with the
        # same uncaught exception.
        for status in statuses.values():
            status.error = repr(exc)
        session.mcp_status = list(statuses.values())
        return

    # Per-server connection errors collected by MCPBackend.start().
    # An entry here is the unambiguous "this server failed" signal —
    # everything else is at worst "unknown".
    for srv_name, exc in mcp_backend.connection_errors.items():
        if srv_name in statuses:
            statuses[srv_name].state = "failed"
            statuses[srv_name].error = str(exc)

    mcp_tools = mcp_backend.list_tools()
    tools_per_server: dict[str, int] = {name: 0 for name in raw_names}
    configured_names = set(raw_names)
    for spec in mcp_tools:
        session.add_tool(spec)
        # MCPBackend names tools as ``<server>.<tool>`` (dot-
        # separated) — that's the canonical form before any backend-
        # specific sanitisation (the Copilot SDK rewrites ``.`` →
        # ``_`` when surfacing them to the model). We credit the
        # tool count to its server using whichever form appears in
        # the spec; ``mcp__<server>__<tool>`` is the alternate
        # convention some bridges use, so accept both.
        owner = ""
        if spec.name.startswith("mcp__"):
            rest = spec.name[5:]
            owner, _, _ = rest.partition("__")
        elif "." in spec.name:
            owner, _, _ = spec.name.partition(".")
        # Fallback: match any configured server whose name prefixes
        # the tool spec name (handles ``server_tool`` after some
        # bridges sanitise dots to underscores before our handler
        # sees them).
        if not owner or owner not in configured_names:
            for cand in configured_names:
                if (
                    spec.name == cand
                    or spec.name.startswith(f"{cand}_")
                    or spec.name.startswith(f"{cand}.")
                ):
                    owner = cand
                    break
        if owner in tools_per_server:
            tools_per_server[owner] += 1

    for name, count in tools_per_server.items():
        if name not in statuses:
            continue
        if count > 0:
            statuses[name].state = "connected"
            statuses[name].tool_count = count
            # Defensive: a connected server with a stale error from a
            # prior attempt would be confusing — clear it.
            statuses[name].error = ""

    if not mcp_tools:
        logger.warning(
            "MCP servers configured but no tools registered. Connection errors: %s",
            mcp_backend.connection_errors,
        )

    # Register for LIFO teardown so the socket closes on session aclose
    session.register_resource(mcp_backend, name="mcp_backend")
    session.mcp_status = list(statuses.values())
    logger.info(
        "install_mcp_servers: configured=%d connected=%d tools=%d",
        len(configs),
        sum(1 for s in statuses.values() if s.state == "connected"),
        len(mcp_tools),
    )
