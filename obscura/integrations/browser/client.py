"""client — Python client for the browser-extension socket bridge.

A separate obscura process (terminal REPL, REST API, headless agent) uses
this client to drive a running browser extension's tools. It auto-discovers
an active native host from the registry at ``~/.obscura/browser/active.json``
and opens the host's Unix socket.

Typical usage
~~~~~~~~~~~~~

::

    from obscura.integrations.browser.client import BrowserBridgeClient

    async with await BrowserBridgeClient.connect() as bridge:
        page = await bridge.call("browser_read_page", {"max_chars": 5_000})

If you want the browser tools registered with an obscura ``ToolRegistry``::

    from obscura.integrations.browser.client import register_browser_tools

    bridge = await register_browser_tools(registry)
    # tools are now callable through the registry; bridge.close() to detach.

The client multiplexes concurrent calls over a single connection. Pending
calls are cancelled if the connection drops.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Any, cast

from obscura.core.types import ToolSpec

from . import active_hosts, wire

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

log = logging.getLogger("obscura.browser.client")


class BrowserBridgeError(RuntimeError):
    """Raised when the bridge can't be reached or returns an error."""


# Patterns we know how to translate into an action hint for the LLM agent.
# Order matters — first match wins. Keep substrings lowercase; we match on
# the lowercased error message. The hint is appended to the original message
# (preserving the raw text so debugging humans still see it).
_ERROR_HINTS: tuple[tuple[str, str], ...] = (
    (
        "no active obscura browser host",
        # Already actionable; nothing to add.
        "",
    ),
    (
        "bridge is closed",
        " (the side panel was closed after this session attached; ask the "
        "user to re-open the Obscura side panel in Chrome, then retry)",
    ),
    (
        "bridge connection closed",
        " (the side panel disconnected mid-call — likely closed or the "
        "browser quit; ask the user to re-open the Obscura side panel)",
    ),
    (
        "bridge connection lost",
        " (the side panel went away — likely closed mid-call; ask the user "
        "to re-open the Obscura side panel in Chrome)",
    ),
    (
        "socket",  # "socket {path} not reachable: ..."
        " (the side panel does not appear to be running; ask the user to "
        "open the Obscura side panel in Chrome before retrying)",
    ),
    (
        "timed out connecting",
        " (could not reach the side panel — it may be busy or unresponsive; "
        "ask the user to verify the Obscura side panel is open and active)",
    ),
    (
        "timed out after",  # "browser bridge call '{name}' timed out after {n}s"
        " (the call did not complete in time — the page may still be "
        "loading, the selector may not exist, or the tab may be unresponsive; "
        "call browser_screenshot or browser_read_page to verify page state "
        "before retrying)",
    ),
    (
        "no match",
        " (selector matched nothing on the active tab; the tab may not be "
        "the one you expect, the page may not have finished loading, or the "
        "selector may be wrong — call browser_screenshot or browser_read_page "
        "to verify before retrying)",
    ),
    (
        "no active tab",
        " (the active tab Chrome reports is not accessible — the user may "
        "have closed it or switched windows; ask them to focus a normal tab)",
    ),
    (
        "debugger already attached",
        " (another Chrome debugger is connected to this tab — usually "
        "DevTools or another extension; ask the user to close DevTools or "
        "the other client, then retry)",
    ),
    (
        "cannot attach to",  # CDP attach error variants
        " (CDP attach was refused — the tab may be a chrome:// URL, the "
        "Web Store, or otherwise off-limits to extensions; switch to a "
        "regular http(s) tab)",
    ),
)


def _diagnostic_for_error(tool_name: str, message: str) -> str:
    """Augment a raw bridge/host error with a tool-name prefix and an
    action hint based on known failure patterns.

    Pure function — safe to unit-test without any socket. Always returns a
    string; if no pattern matches, returns ``"{tool_name}: {message}"`` so
    the LLM at least knows which tool produced the error.
    """
    msg = message or "unknown error"
    lowered = msg.lower()
    hint = ""
    for needle, suffix in _ERROR_HINTS:
        if needle in lowered:
            hint = suffix
            break
    prefix = f"{tool_name}: " if tool_name else ""
    return f"{prefix}{msg}{hint}"


class BrowserBridgeClient:
    """Async client for the native host's socket bridge.

    Construct via ``BrowserBridgeClient.connect()`` — this resolves a live
    host from the registry and opens the connection. After construction,
    ``call(name, args)`` dispatches a single tool call and awaits its
    result. ``close()`` releases the connection; the class is also an
    async context manager.
    """

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        host_entry: active_hosts.HostEntry,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self.host_entry = host_entry
        self._pending: dict[str, asyncio.Future[Any]] = {}
        self._reader_task: asyncio.Task[None] | None = None
        self._closed = False
        self._tools_cache: list[dict[str, Any]] | None = None

    # -- construction -------------------------------------------------------

    @classmethod
    async def connect(
        cls,
        *,
        profile_id: str | None = None,
        browser: str | None = None,
        socket_path: str | Path | None = None,
        timeout: float = 5.0,
    ) -> BrowserBridgeClient:
        """Discover an active host and open a connection.

        ``socket_path`` overrides discovery (useful for tests). Otherwise
        the most-recent live host matching ``profile_id`` and ``browser``
        is selected; if none match the filters, the most-recent live host
        is used.
        """
        if socket_path is not None:
            entry: active_hosts.HostEntry = {
                "pid": 0,
                "socket": str(socket_path),
                "profile_id": None,
                "browser": None,
                "version": "",
                "started_at": 0.0,
            }
        else:
            entry_opt = active_hosts.pick(profile_id=profile_id, browser=browser)
            if entry_opt is None:
                entry_opt = active_hosts.pick()
            if entry_opt is None:
                msg = (
                    "no active obscura browser host found — open the side panel "
                    "in Chrome to start one"
                )
                raise BrowserBridgeError(msg)
            entry = entry_opt

        path = str(entry.get("socket") or "")
        if not path:
            msg = f"active host entry has no socket path: {entry!r}"
            raise BrowserBridgeError(msg)

        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(path), timeout=timeout
            )
        except (FileNotFoundError, ConnectionRefusedError) as e:
            msg = f"socket {path} not reachable: {e}"
            raise BrowserBridgeError(msg) from e
        except TimeoutError as e:
            msg = f"timed out connecting to {path}"
            raise BrowserBridgeError(msg) from e

        client = cls(reader, writer, entry)
        client._reader_task = asyncio.create_task(  # noqa: SLF001
            client._read_loop(), name="browser-bridge-reader"
        )
        try:
            await asyncio.wait_for(client._handshake(), timeout=timeout)
        except Exception:
            await client.close()
            raise
        return client

    async def _handshake(self) -> None:
        await self._send({"type": "hello"})
        # The reader loop dispatches to a handshake future via type "hello".
        # We use a one-shot future keyed under "__hello__".
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Any] = loop.create_future()
        self._pending["__hello__"] = fut
        try:
            ack_raw: Any = await fut
        finally:
            self._pending.pop("__hello__", None)
        if not isinstance(ack_raw, dict):
            msg = f"unexpected handshake reply: {ack_raw!r}"
            raise BrowserBridgeError(msg)
        ack = cast("dict[str, Any]", ack_raw)
        if ack.get("type") != "hello":
            msg = f"unexpected handshake reply: {ack!r}"
            raise BrowserBridgeError(msg)

    # -- API ----------------------------------------------------------------

    async def list_tools(self, *, refresh: bool = False) -> list[dict[str, Any]]:
        """Return tool specs the host is offering. Cached after first call."""
        if self._tools_cache is not None and not refresh:
            return self._tools_cache
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Any] = loop.create_future()
        self._pending["__tools__"] = fut
        await self._send({"type": "list_tools"})
        try:
            reply_raw: Any = await fut
        finally:
            self._pending.pop("__tools__", None)
        if not isinstance(reply_raw, dict):
            msg = f"bad tools reply: {reply_raw!r}"
            raise BrowserBridgeError(msg)
        reply = cast("dict[str, Any]", reply_raw)
        tools_raw = reply.get("tools")
        if not isinstance(tools_raw, list):
            msg = f"bad tools reply: {reply!r}"
            raise BrowserBridgeError(msg)
        items = cast("list[Any]", tools_raw)
        tools = [cast("dict[str, Any]", t) for t in items if isinstance(t, dict)]
        self._tools_cache = tools
        return tools

    async def call(
        self,
        name: str,
        args: dict[str, Any] | None = None,
        *,
        timeout: float | None = 60.0,
    ) -> Any:
        """Dispatch a tool call. Returns the tool's value or raises."""
        if self._closed:
            msg = "bridge is closed"
            raise BrowserBridgeError(msg)
        cid = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Any] = loop.create_future()
        self._pending[cid] = fut
        await self._send({"type": "call", "id": cid, "name": name, "args": args or {}})
        try:
            return (
                await asyncio.wait_for(fut, timeout=timeout) if timeout else await fut
            )
        except TimeoutError as e:
            msg = f"browser bridge call '{name}' timed out after {timeout}s"
            raise BrowserBridgeError(msg) from e
        finally:
            self._pending.pop(cid, None)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(BrowserBridgeError("bridge closed"))
        self._pending.clear()
        with contextlib.suppress(Exception):
            self._writer.close()
            await self._writer.wait_closed()
        if self._reader_task is not None:
            self._reader_task.cancel()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await self._reader_task

    async def __aenter__(self) -> BrowserBridgeClient:
        return self

    async def __aexit__(
        self,
        _t: type[BaseException] | None,
        _v: BaseException | None,
        _tb: TracebackType | None,
    ) -> None:
        await self.close()

    # -- internals ----------------------------------------------------------

    async def _send(self, frame: dict[str, Any]) -> None:
        try:
            self._writer.write(wire.encode_frame(frame))
            await self._writer.drain()
        except (ConnectionResetError, BrokenPipeError) as e:
            msg = f"bridge connection lost: {e}"
            raise BrowserBridgeError(msg) from e

    async def _read_loop(self) -> None:
        try:
            while True:
                frame = await wire.read_frame(self._reader)
                if frame is None:
                    break
                self._dispatch(frame)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("browser bridge reader crashed")
        finally:
            for fut in list(self._pending.values()):
                if not fut.done():
                    fut.set_exception(BrowserBridgeError("bridge connection closed"))
            self._pending.clear()

    def _dispatch(self, frame: dict[str, Any]) -> None:
        t = str(frame.get("type") or "")
        if t == "hello":
            fut = self._pending.get("__hello__")
            if fut is not None and not fut.done():
                fut.set_result(frame)
        elif t == "tools":
            fut = self._pending.get("__tools__")
            if fut is not None and not fut.done():
                fut.set_result(frame)
        elif t == "result":
            cid = str(frame.get("id") or "")
            fut = self._pending.get(cid)
            if fut is not None and not fut.done():
                fut.set_result(frame.get("value"))
        elif t == "error":
            cid = str(frame.get("id") or "")
            fut = self._pending.get(cid)
            if fut is not None and not fut.done():
                fut.set_exception(
                    BrowserBridgeError(str(frame.get("message") or "unknown error"))
                )
        elif t == "pong":
            pass
        else:
            log.debug("bridge client got unknown frame: %r", t)


# ---------------------------------------------------------------------------
# Helper: register browser tools through any tool-registration callable.


def _build_proxy_spec(
    raw: dict[str, Any],
    client: BrowserBridgeClient,
    *,
    name_prefix: str = "",
) -> ToolSpec:
    """Turn a wire-format tool descriptor into a real ``ToolSpec`` whose
    handler proxies through the given bridge client."""
    real_name = str(raw.get("name") or "")
    name = name_prefix + real_name
    description = str(raw.get("description") or "")
    params_raw = raw.get("parameters")
    params: dict[str, Any] = (
        cast("dict[str, Any]", params_raw)
        if isinstance(params_raw, dict)
        else {"type": "object", "properties": {}}
    )
    side_effects = str(raw.get("side_effects") or "unknown")

    async def _handler(**kwargs: Any) -> Any:
        try:
            return await client.call(real_name, kwargs)
        except BrowserBridgeError as e:
            # Wrap every bridge/host error with the tool name and an action
            # hint so the LLM agent knows what to do, not just what failed.
            raise BrowserBridgeError(
                _diagnostic_for_error(real_name, str(e))
            ) from e

    return ToolSpec(
        name=name,
        description=description,
        parameters=params,
        handler=cast("Callable[..., Awaitable[Any]]", _handler),
        side_effects=cast("Any", side_effects),
    )


async def register_browser_tools(
    register_fn: Callable[[ToolSpec], None],
    *,
    profile_id: str | None = None,
    browser: str | None = None,
    name_prefix: str = "",
) -> BrowserBridgeClient:
    """Connect to an active host and register every browser tool.

    ``register_fn`` accepts a ``ToolSpec`` — pass ``ToolRegistry.register`` or
    ``ObscuraClient.register_tool`` (or any callable matching that shape).

    Returns the open ``BrowserBridgeClient`` — the caller is responsible for
    calling ``client.close()`` when done (or using it as an async context
    manager). Tools registered here proxy through the bridge; if the
    connection drops, calls raise ``BrowserBridgeError``.

    ``name_prefix`` lets callers namespace tools (e.g. ``"ext_"``) when the
    bridge tools collide with names already in the registry.
    """
    client = await BrowserBridgeClient.connect(profile_id=profile_id, browser=browser)
    try:
        specs = await client.list_tools()
    except Exception:
        await client.close()
        raise

    for spec in specs:
        register_fn(_build_proxy_spec(spec, client, name_prefix=name_prefix))

    return client


async def attach_if_running(
    register_fn: Callable[[ToolSpec], None],
    *,
    profile_id: str | None = None,
    browser: str | None = None,
    name_prefix: str = "",
) -> tuple[BrowserBridgeClient | None, dict[str, Any] | None]:
    """Best-effort auto-attach for REPL bootstrap.

    Returns ``(client, status)`` where:
    - ``client`` is an open :class:`BrowserBridgeClient` if a host was found
      and tools were registered, otherwise ``None``.
    - ``status`` is a small dict suitable for status-line rendering:
      ``{"browser": "chrome", "profile_id": "...", "tool_count": 18,
         "socket": "/tmp/obscura-browser/.../1234.sock"}``.
      ``None`` when no host is running.

    Never raises — errors are logged and swallowed so a missing or unhealthy
    extension can never block the REPL from starting.
    """
    from . import active_hosts

    entry = active_hosts.pick(profile_id=profile_id, browser=browser)
    if entry is None:
        entry = active_hosts.pick()
    if entry is None:
        return None, None

    try:
        client = await register_browser_tools(
            register_fn,
            profile_id=profile_id,
            browser=browser,
            name_prefix=name_prefix,
        )
    except BrowserBridgeError as e:
        log.info("browser bridge present but unreachable: %s", e)
        return None, None
    except Exception:
        log.exception("browser auto-attach failed")
        return None, None

    # list_tools() is cached after register_browser_tools — this is a no-op
    # network call.
    try:
        tools = await client.list_tools()
    except Exception:
        log.debug("suppressed exception in attach_if_running", exc_info=True)
        tools = []
    status: dict[str, Any] = {
        "browser": entry.get("browser"),
        "profile_id": entry.get("profile_id"),
        "tool_count": len(tools),
        "socket": entry.get("socket"),
        "pid": entry.get("pid"),
    }
    return client, status


__all__ = [
    "BrowserBridgeClient",
    "BrowserBridgeError",
    "_build_proxy_spec",
    "attach_if_running",
    "register_browser_tools",
]
