"""socket_bridge — Unix-socket fan-out so any local obscura process can drive
the browser via a single running native host.

Architecture
------------
The native host has exactly one connection to Chrome (stdin/stdout). But many
*separate* obscura processes (terminal REPL, REST API, headless agents) may
want to call browser tools at the same time. This module exposes a small
length-prefixed JSON-RPC server on a Unix socket so each of those processes
can connect, list tools, and dispatch calls — all of which fan out into
the existing :func:`browser_tools._call` path.

Wire format
~~~~~~~~~~~
4-byte little-endian length prefix + UTF-8 JSON body (same framing as
Chrome native messaging — symmetry keeps the framing code identical).

::

  client  → host  {"type":"hello"}
  host    → client {"type":"hello","version":...,"pid":...,"profile_id":...,
                    "tool_count":N}
  client  → host  {"type":"list_tools"}
  host    → client {"type":"tools","tools":[{name,description,parameters,
                                              side_effects},...]}
  client  → host  {"type":"call","id":"<client-id>","name":"browser_read_page",
                    "args":{...}}
  host    → client {"type":"result","id":"<client-id>","ok":true,"value":...}
                  | {"type":"error","id":"<client-id>","message":"..."}

The ``id`` is opaque to the host; each client keeps its own ID space.

Per-call concurrency: each ``call`` frame is dispatched in its own task, so a
slow tool doesn't block the connection's read loop. When a client disconnects
mid-call the in-flight task is cancelled.

Path & ownership
~~~~~~~~~~~~~~~~
Socket lives at ``/tmp/obscura-browser/<user>/<pid>.sock``. The parent dir
is created with mode 0700 and the socket is unlinked on host exit. PID-scoped
paths mean multiple Chrome profiles (and therefore multiple hosts) coexist
without contention.
"""

from __future__ import annotations

import asyncio
import contextlib
import getpass
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from obscura.integrations.browser.wire import encode_frame, read_frame

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

log = logging.getLogger("obscura.browser.socket_bridge")

VERSION = "0.1.0"


def default_socket_dir() -> Path:
    """Return the per-user socket directory, creating it with mode 0700.

    Centralised so tests can stub the path with ``OBSCURA_BROWSER_SOCKET_DIR``.
    """
    override = os.environ.get("OBSCURA_BROWSER_SOCKET_DIR")
    if override:
        d = Path(override)
    else:
        try:
            user = getpass.getuser()
        except Exception:
            user = str(os.getuid()) if hasattr(os, "getuid") else "default"
        d = Path("/tmp") / "obscura-browser" / user
    d.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        d.chmod(0o700)
    return d


def default_socket_path(pid: int | None = None) -> Path:
    return default_socket_dir() / f"{pid or os.getpid()}.sock"


# ---------------------------------------------------------------------------
# Bridge


class SocketBridge:
    """Async Unix-socket server fanning tool calls into browser_tools.

    The bridge is host-agnostic — it takes a ``call`` callable and a
    ``tools_provider`` callable so unit tests can drive it without spinning up
    the entire native host.
    """

    def __init__(
        self,
        *,
        path: Path,
        tools_provider: Callable[[], list[dict[str, Any]]],
        call: Callable[[str, dict[str, Any]], Awaitable[Any]],
        profile_id: Callable[[], str | None] | None = None,
    ) -> None:
        self.path = path
        self._tools_provider = tools_provider
        self._call = call
        self._profile_id = profile_id or (lambda: None)
        self._server: asyncio.base_events.Server | None = None
        # Track tasks per writer so we can cancel them on disconnect.
        self._inflight: dict[int, set[asyncio.Task[None]]] = {}

    async def start(self) -> None:
        # Stale socket from a crashed predecessor — clean up before bind.
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()
        self._server = await asyncio.start_unix_server(
            self._handle_client, path=str(self.path)
        )
        with contextlib.suppress(OSError):
            os.chmod(self.path, 0o600)
        log.info("socket bridge listening at %s", self.path)

    async def stop(self) -> None:
        srv = self._server
        self._server = None
        if srv is not None:
            srv.close()
            with contextlib.suppress(Exception):
                await srv.wait_closed()
        # Cancel any still-running per-client tasks.
        for tasks in list(self._inflight.values()):
            for t in tasks:
                t.cancel()
        self._inflight.clear()
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = id(writer)
        self._inflight[peer] = set()
        log.info("socket client connected: peer=%s", peer)
        try:
            while True:
                try:
                    msg = await read_frame(reader)
                except asyncio.IncompleteReadError:
                    break
                if msg is None:
                    break
                msg_type = str(msg.get("type") or "")

                if msg_type == "hello":
                    await self._send(
                        writer,
                        {
                            "type": "hello",
                            "version": VERSION,
                            "pid": os.getpid(),
                            "profile_id": self._profile_id(),
                            "tool_count": len(self._tools_provider()),
                        },
                    )
                elif msg_type == "list_tools":
                    await self._send(
                        writer,
                        {"type": "tools", "tools": self._tools_provider()},
                    )
                elif msg_type == "call":
                    cid = str(msg.get("id") or "")
                    name = str(msg.get("name") or "")
                    raw_args: Any = msg.get("args") or {}
                    if not isinstance(raw_args, dict):
                        await self._send(
                            writer,
                            {
                                "type": "error",
                                "id": cid,
                                "message": "args must be an object",
                            },
                        )
                        continue
                    args = cast("dict[str, Any]", raw_args)
                    task = asyncio.create_task(
                        self._dispatch(writer, cid, name, args)
                    )
                    self._inflight[peer].add(task)
                    task.add_done_callback(
                        lambda t, p=peer: self._inflight.get(p, set()).discard(t)
                    )
                elif msg_type == "ping":
                    await self._send(writer, {"type": "pong", "id": msg.get("id")})
                else:
                    log.debug("socket client sent unknown frame: %r", msg_type)
        except Exception:
            log.exception("socket client handler crashed")
        finally:
            for t in list(self._inflight.get(peer, ())):
                t.cancel()
            self._inflight.pop(peer, None)
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()
            log.info("socket client disconnected: peer=%s", peer)

    async def _dispatch(
        self,
        writer: asyncio.StreamWriter,
        cid: str,
        name: str,
        args: dict[str, Any],
    ) -> None:
        try:
            value = await self._call(name, args)
            await self._send(
                writer, {"type": "result", "id": cid, "ok": True, "value": value}
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            await self._send(
                writer,
                {"type": "error", "id": cid, "message": f"{type(e).__name__}: {e}"},
            )

    async def _send(self, writer: asyncio.StreamWriter, obj: dict[str, Any]) -> None:
        try:
            writer.write(encode_frame(obj))
            await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            log.debug("socket peer went away mid-write", exc_info=True)
        except Exception:
            log.exception("socket write failed")


__all__ = [
    "VERSION",
    "SocketBridge",
    "default_socket_dir",
    "default_socket_path",
]
