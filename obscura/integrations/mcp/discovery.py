"""obscura.integrations.mcp.discovery — Probe external MCP servers for their tools.

External MCP servers configured under ``mcp_servers`` in obscura's session
config are passed through to Claude SDK directly: the SDK handles their
invocation. But obscura's own ToolRegistry never learned about them, which
meant:

  * The system prompt's tool listing didn't include them.
  * ``tool_search`` couldn't find them.
  * The model had to guess names and frequently hallucinated the wrong
    namespace prefix (``mcp__obs__*`` — obscura's own server — instead of
    ``mcp__<server>__*``).

This module fixes that by connecting to each external server at session
build time, listing its tools, and registering *shadow* :class:`ToolSpec`
entries in the registry. The shadows exist only for discovery — Claude
SDK still routes the actual calls through the original ``mcp_servers``
passthrough. The shadow handler returns a clear error if invoked
directly, which would indicate a routing bug.

Discovery results are surfaced as :class:`DiscoveryReport` so failures
don't vanish into log lines — backends store the report on
``last_mcp_discovery_report`` and the ``mcp_discovery_status`` system
tool exposes it from inside an agent session.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from obscura.integrations.mcp.client import MCPClient
from obscura.integrations.mcp.types import MCPConnectionConfig, MCPTransportType

if TYPE_CHECKING:
    from obscura.core.types import ToolSpec

logger = logging.getLogger(__name__)

# Per-server probe timeout. Keep short so a slow / dead MCP server doesn't
# stall session startup.
_DEFAULT_PROBE_TIMEOUT = 3.0


@dataclass(frozen=True)
class DiscoveryStatus:
    """Outcome of probing one external MCP server.

    Captured per-server so callers can show *which* servers failed and
    *why* — log lines alone made it hard to tell whether the prognostic
    binary was missing or whether the server timed out.
    """

    server_name: str
    transport: str
    ok: bool
    tool_count: int
    error: str | None = None
    duration_ms: int = 0


@dataclass(frozen=True)
class DiscoveryReport:
    """Aggregate outcome of probing every configured MCP server."""

    statuses: tuple[DiscoveryStatus, ...] = field(default_factory=tuple)
    specs: tuple[ToolSpec, ...] = field(default_factory=tuple)

    @property
    def total_tools(self) -> int:
        return sum(s.tool_count for s in self.statuses)

    @property
    def ok_servers(self) -> tuple[DiscoveryStatus, ...]:
        return tuple(s for s in self.statuses if s.ok)

    @property
    def failed_servers(self) -> tuple[DiscoveryStatus, ...]:
        return tuple(s for s in self.statuses if not s.ok)

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable view, suitable for the ``mcp_discovery_status`` tool."""
        return {
            "ok": all(s.ok for s in self.statuses) if self.statuses else True,
            "total_tools": self.total_tools,
            "servers": [
                {
                    "name": s.server_name,
                    "transport": s.transport,
                    "ok": s.ok,
                    "tool_count": s.tool_count,
                    "error": s.error,
                    "duration_ms": s.duration_ms,
                }
                for s in self.statuses
            ],
        }


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


def build_mcp_connection_config(server: dict[str, Any]) -> MCPConnectionConfig | None:
    """Translate an obscura mcp_servers entry into an MCPConnectionConfig.

    Returns None if the entry is malformed (e.g. missing command for stdio).
    """
    transport_str = str(server.get("transport", "stdio")).lower()
    name = str(server.get("name") or "")
    server_timeout = _resolve_server_timeout(server)

    if transport_str == "stdio":
        command = server.get("command")
        if not command:
            return None
        return MCPConnectionConfig(
            transport=MCPTransportType.STDIO,
            command=str(command),
            args=list(server.get("args") or []),
            env=dict(server.get("env") or {}),
            timeout=server_timeout,
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
            timeout=server_timeout,
            name=name,
        )

    return None


# Backwards-compatible private alias — keeps direct test references working
# (``discovery._build_config(...)``) while new callers use the public name.
_build_config = build_mcp_connection_config


def _resolve_server_timeout(server: dict[str, Any]) -> float:
    """Pick the discovery timeout for one server.

    Reads ``timeout`` or ``timeout_seconds`` from the server config, falls
    back to ``_DEFAULT_PROBE_TIMEOUT``. Slow MCP servers (prediction-market
    APIs, long-running scrapers) can override per-server without bumping
    the default for fast ones.
    """
    raw = server.get("timeout")
    if raw is None:
        raw = server.get("timeout_seconds")
    if raw is None:
        return _DEFAULT_PROBE_TIMEOUT
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return _DEFAULT_PROBE_TIMEOUT
    # Clamp to a sane window — negative timeouts make no sense; very long
    # ones would stall session startup.
    return max(0.5, min(value, 60.0))


async def _probe_one_server(
    server: dict[str, Any],
    *,
    timeout: float,
) -> tuple[list[ToolSpec], DiscoveryStatus]:
    """Connect to one MCP server, list tools, return shadow ToolSpecs + status.

    Failures yield an empty spec list and a :class:`DiscoveryStatus` with
    ``ok=False`` and the error message. Discovery never raises — the
    status is the channel for surfacing what went wrong.

    The effective timeout is ``max(timeout, server.get("timeout"))`` —
    per-server overrides extend the window for known-slow servers but
    can't shrink it below the caller's floor.
    """
    from obscura.core.types import ToolSpec  # local to avoid import cycle

    name = str(server.get("name") or "unknown")
    transport = str(server.get("transport") or "stdio")
    started = time.monotonic()

    config = build_mcp_connection_config(server)
    if config is None:
        logger.warning("MCP discovery: malformed config for %s", name)
        return [], DiscoveryStatus(
            server_name=name,
            transport=transport,
            ok=False,
            tool_count=0,
            error="malformed config (missing command or url)",
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    # Honour per-server timeout overrides — slow servers (e.g. external
    # prediction-market APIs that hit a remote orderbook) can declare a
    # longer probe timeout without inflating the global default.
    effective_timeout = max(timeout, config.timeout)

    try:
        async with asyncio.timeout(effective_timeout + 1.0):
            async with MCPClient(config) as client:
                tools = await client.list_tools()
    except (TimeoutError, Exception) as exc:
        logger.warning("MCP discovery failed for %s: %s", name, exc)
        return [], DiscoveryStatus(
            server_name=name,
            transport=transport,
            ok=False,
            tool_count=0,
            error=f"{type(exc).__name__}: {exc}",
            duration_ms=int((time.monotonic() - started) * 1000),
        )

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
    return specs, DiscoveryStatus(
        server_name=name,
        transport=transport,
        ok=True,
        tool_count=len(specs),
        duration_ms=int((time.monotonic() - started) * 1000),
    )


async def register_external_mcp_tools(
    backend: Any,
    mcp_servers: list[dict[str, Any]],
    *,
    timeout: float = _DEFAULT_PROBE_TIMEOUT,
) -> DiscoveryReport:
    """Discover *mcp_servers* and register the shadow specs into *backend*.

    *backend* must expose ``register_tool(spec)``. The full
    :class:`DiscoveryReport` is returned and stored on
    ``backend.last_mcp_discovery_report`` (when the backend uses
    :class:`BackendToolHostMixin`), so callers and the
    ``mcp_discovery_status`` system tool can inspect failures
    after the fact rather than scraping log lines.

    Also scans for leaked subprocesses (Claude SDK doesn't always reap
    its stdio MCP servers when sessions end abruptly) and logs a warning
    naming each server with more than one matching process. We never
    auto-kill — concurrent sessions in another shell may legitimately own
    those processes; cleanup is opt-in via the ``mcp_cleanup_orphans`` tool.
    """
    if not mcp_servers:
        report = DiscoveryReport()
        _set_last_report(backend, report)
        return report

    _warn_about_leaked_processes(mcp_servers)

    try:
        report = await discover_mcp_tools_with_report(mcp_servers, timeout=timeout)
    except Exception as exc:
        logger.warning("MCP discovery aborted: %s", exc)
        report = DiscoveryReport(
            statuses=(
                DiscoveryStatus(
                    server_name=str(s.get("name") or "unknown"),
                    transport=str(s.get("transport") or "stdio"),
                    ok=False,
                    tool_count=0,
                    error=f"{type(exc).__name__}: {exc}",
                )
                for s in mcp_servers
            ),
        )
        _set_last_report(backend, report)
        return report

    register = getattr(backend, "register_tool", None)
    if register is not None:
        for spec in report.specs:
            try:
                register(spec)
            except Exception as exc:
                logger.warning("Failed to register shadow spec %s: %s", spec.name, exc)

    _set_last_report(backend, report)
    return report


def _set_last_report(backend: Any, report: DiscoveryReport) -> None:
    """Stash *report* on the backend if it accepts the attribute."""
    try:
        backend.last_mcp_discovery_report = report
    except Exception:
        # Backend doesn't support the attribute — that's fine, the report is
        # still returned to the caller.
        pass


def _warn_about_leaked_processes(mcp_servers: list[dict[str, Any]]) -> None:
    """Log a warning for each server with multiple existing subprocesses.

    Single matches are normal — Claude SDK reuses its subprocess across a
    session. Two or more usually means an earlier session leaked.
    """
    try:
        from obscura.integrations.mcp.process_cleanup import detect_orphans
    except ImportError:
        return

    try:
        orphans = detect_orphans(mcp_servers)
    except Exception as exc:
        logger.debug("MCP orphan scan failed: %s", exc)
        return

    for name, procs in orphans.items():
        if len(procs) <= 1:
            continue
        stopped = [p for p in procs if p.is_stopped]
        logger.warning(
            "MCP server '%s' has %d running subprocess(es) — likely leak from "
            "previous session(s). PIDs: %s%s. Use the 'mcp_cleanup_orphans' "
            "tool to reap them.",
            name,
            len(procs),
            ",".join(str(p.pid) for p in procs),
            f" ({len(stopped)} suspended)" if stopped else "",
        )


async def discover_mcp_tools(
    mcp_servers: list[dict[str, Any]],
    *,
    timeout: float = _DEFAULT_PROBE_TIMEOUT,
) -> list[ToolSpec]:
    """Probe every server and return the flat list of shadow ToolSpecs.

    Thin wrapper around :func:`discover_mcp_tools_with_report` that
    discards the per-server status info — kept for callers that only
    want the specs (and existing tests).
    """
    report = await discover_mcp_tools_with_report(mcp_servers, timeout=timeout)
    return list(report.specs)


async def discover_mcp_tools_with_report(
    mcp_servers: list[dict[str, Any]],
    *,
    timeout: float = _DEFAULT_PROBE_TIMEOUT,
) -> DiscoveryReport:
    """Probe every server concurrently and return a :class:`DiscoveryReport`.

    Each server has its own timeout, so one slow / dead server doesn't
    block the others. Per-server failures are captured in the report's
    ``statuses`` tuple — never raised. A gather-level exception (rare,
    typically programmer error) is treated the same way.
    """
    if not mcp_servers:
        return DiscoveryReport()

    raw = await asyncio.gather(
        *(_probe_one_server(s, timeout=timeout) for s in mcp_servers),
        return_exceptions=True,
    )

    specs: list[ToolSpec] = []
    statuses: list[DiscoveryStatus] = []
    for cfg, result in zip(mcp_servers, raw, strict=True):
        if isinstance(result, tuple):
            tool_specs, status = result
            specs.extend(tool_specs)
            statuses.append(status)
        else:
            # Unexpected — _probe_one_server should never raise.
            name = str(cfg.get("name") or "unknown")
            transport = str(cfg.get("transport") or "stdio")
            logger.warning("MCP discovery task raised for %s: %s", name, result)
            statuses.append(
                DiscoveryStatus(
                    server_name=name,
                    transport=transport,
                    ok=False,
                    tool_count=0,
                    error=f"{type(result).__name__}: {result}",
                ),
            )

    return DiscoveryReport(statuses=tuple(statuses), specs=tuple(specs))


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
