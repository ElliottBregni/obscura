# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false
"""browser_mcp_server — in-process MCP server exposing the browser tools.

Why this exists
---------------
The Claude and Copilot backends route tool calls through Obscura's own
tool executor, so ``register_tool()`` is enough to wire up
``browser_tools.TOOLS``.  The Codex SDK runs its own closed tool loop
and only calls tools it discovered through its native ``mcp_servers``
config.  Codex's ``mcp_servers`` entries are either a subprocess (stdio
transport) or a streamable-HTTP URL.  The browser tools can't be moved
to a subprocess because their handlers close over the native host's
``_write_frame`` — so we expose them via streamable-HTTP inside the
host process itself.

Usage
-----
::

    from browser_mcp_server import start_browser_mcp, stop_browser_mcp

    # During host startup, when the first Codex session is created:
    url = await start_browser_mcp()
    # url == "http://127.0.0.1:<port>/mcp"

    # Pass url into the Codex session's extra_mcp_servers list.

    # On host shutdown:
    await stop_browser_mcp()

The server binds to 127.0.0.1 only — never reachable from other
machines.  It is idempotent: calling ``start_browser_mcp`` more than
once returns the same URL until ``stop_browser_mcp`` has been called.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from typing import TYPE_CHECKING, Any

import uvicorn
from mcp.server.fastmcp import FastMCP

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

log = logging.getLogger("obscura.browser_mcp_server")


# Module-level singleton state.  The host only needs one server regardless
# of how many Codex sessions run through it in the panel's lifetime.
# ``uvicorn.Server`` has no static type stubs shipped with the package,
# so we annotate as ``Any`` to keep strict type-checkers quiet.
_server: Any | None = None
_serve_task: asyncio.Task[None] | None = None
_url: str | None = None


async def start_browser_mcp(
    *,
    host: str = "127.0.0.1",
    ready_timeout: float = 5.0,
) -> str:
    """Start the streamable-HTTP MCP server and return its URL.

    Idempotent — subsequent calls return the already-serving URL.
    """
    global _server, _serve_task, _url
    if _url is not None:
        return _url

    port = _pick_free_port(host)
    app = _build_fastmcp_app()

    # Quiet uvicorn — the native host logs to a file and Chrome's SW
    # captures its own output; uvicorn's per-request noise adds nothing.
    config: Any = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="warning",
        access_log=False,
        lifespan="on",
    )
    server: Any = uvicorn.Server(config)
    # Uvicorn normally installs a signal handler for clean shutdown, but
    # the native host already owns the process lifecycle — we stop the
    # server explicitly via stop_browser_mcp().
    server.install_signal_handlers = lambda: None
    _server = server

    _serve_task = asyncio.create_task(server.serve(), name="browser-mcp-server")

    # serve() is an infinite coroutine; wait for uvicorn's `started` flag
    # so callers don't race the first connection.
    try:
        await asyncio.wait_for(_wait_started(server), timeout=ready_timeout)
    except TimeoutError:
        await stop_browser_mcp()
        msg = f"browser MCP server failed to start within {ready_timeout}s"
        raise RuntimeError(msg) from None

    _url = f"http://{host}:{port}/mcp"
    log.info("browser MCP server listening at %s", _url)
    return _url


async def stop_browser_mcp() -> None:
    """Gracefully shut the MCP server down. Idempotent."""
    global _server, _serve_task, _url
    srv = _server
    task = _serve_task
    _server = None
    _serve_task = None
    _url = None
    if srv is None:
        return
    srv.should_exit = True
    if task is not None:
        try:
            await asyncio.wait_for(task, timeout=3.0)
        except (TimeoutError, asyncio.CancelledError):
            task.cancel()
            with _suppress_exc():
                await task


def current_url() -> str | None:
    """Return the current server URL, or None if not running."""
    return _url


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _pick_free_port(host: str) -> int:
    """Ask the OS for an available TCP port on ``host``.

    There's a small TOCTOU window between closing the probe socket and
    uvicorn binding its own — on localhost with no other contender this
    is effectively race-free, but callers should be prepared for
    ``OSError`` from serve() in pathological cases.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((host, 0))
        return probe.getsockname()[1]


def _build_fastmcp_app() -> Any:
    """Create a FastMCP instance populated with the browser tools."""
    # Lazy import so this module can be unit-tested without browser_tools
    # being on sys.path.
    import browser_tools

    mcp = FastMCP("obscura-browser", stateless_http=True)

    for spec in browser_tools.TOOLS:
        # Each ToolSpec.handler is already a typed async function with a
        # Python signature matching its JSON schema — FastMCP derives the
        # MCP tool schema from it.  We rename to the canonical public
        # ``browser_*`` name so Codex sees the same names Obscura uses.
        handler = _typed_handler(spec)
        mcp.add_tool(
            fn=handler,
            name=spec.name,
            description=spec.description,
        )

    return mcp.streamable_http_app()


def _typed_handler(spec: Any) -> Callable[..., Awaitable[Any]]:
    """Return the ToolSpec handler with a name matching the public tool name.

    The native host's private helpers are prefixed with ``_browser_``; we
    keep the signature (so FastMCP can derive a schema) but expose the
    function under the public name to avoid leaking a leading underscore
    into the MCP tool registry.
    """
    fn = spec.handler
    # A shallow wrapper preserves the signature and keeps the tool name
    # consistent even if the underlying handler is later renamed.
    original = fn

    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        return await original(*args, **kwargs)

    wrapper.__name__ = spec.name
    wrapper.__doc__ = spec.description
    # FastMCP reads `__signature__` / `__annotations__` for schema
    # generation; inherit both from the original typed function.
    wrapper.__signature__ = _inherit_signature(original)  # type: ignore[attr-defined]
    wrapper.__annotations__ = dict(getattr(original, "__annotations__", {}))
    return wrapper


def _inherit_signature(fn: Callable[..., Any]) -> Any:
    import inspect

    return inspect.signature(fn)


async def _wait_started(server: Any) -> None:
    """Poll uvicorn's ``started`` flag until it flips true."""
    while not server.started:
        await asyncio.sleep(0.02)


class _suppress_exc:
    """Tiny context manager used only during shutdown."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, _t: Any, _v: Any, _tb: Any) -> bool:
        return True


__all__ = [
    "current_url",
    "start_browser_mcp",
    "stop_browser_mcp",
]
