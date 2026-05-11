"""obscura.integrations.network_gateway.connections — Live WebSocket connection registry.

A single process-level :class:`ConnectionRegistry` instance is shared by the
main gateway WS endpoint (``/v1/chat/ws``, port 18790) and the standalone
agent WS endpoint (``/ws``, port 18792).  It enables:

* Presence tracking — know how many clients are connected and from where.
* Broadcast — send a frame to every connected client in one call.
* Channel fanout — a single asyncio task drains the ``channel_inject``
  subscription queue and broadcasts ``{"type": "incoming", ...}`` frames to
  all clients, replacing the fragile per-connection subscribe/unsubscribe.
* Shared reply context — the most-recently received platform ``reply_fn``
  is stored here so any client's next message can trigger the platform reply.

Usage::

    from obscura.integrations.network_gateway.connections import get_registry

    registry = get_registry()
    conn_id = await registry.register(websocket, endpoint="/ws", remote="1.2.3.4")
    try:
        await registry.broadcast({"type": "presence", "event": "connected",
                                   "conn_id": conn_id, "count": registry.count})
        ...
    finally:
        await registry.unregister(conn_id)
        await registry.broadcast({"type": "presence", "event": "disconnected",
                                   "conn_id": conn_id, "count": registry.count})
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine

from fastapi import WebSocket

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Protocol version constants
# ---------------------------------------------------------------------------

PROTOCOL_VERSION: int = 1  # current server protocol version
PROTOCOL_MIN_SUPPORTED: int = 1  # oldest version we accept

ReplyFn = Callable[[str], Coroutine[Any, Any, None]]


@dataclass
class ConnectedClient:
    """Metadata for a single live WebSocket client."""

    conn_id: str
    websocket: WebSocket
    endpoint: str = "/ws"  # which endpoint path this came from
    remote: str = ""  # best-effort IP


class ConnectionRegistry:
    """Process-level registry of all active WebSocket clients.

    Thread-safe via an :class:`asyncio.Lock`.  All public methods are
    ``async`` and must be called from within the running event loop.
    """

    def __init__(self) -> None:
        self._clients: dict[str, ConnectedClient] = {}
        self._lock = asyncio.Lock()
        # Shared active reply context — set when a platform message arrives,
        # cleared after the next agent response fires the callback.
        self._active_reply: ReplyFn | None = None
        self._fanout_task: asyncio.Task[None] | None = None
        self._presence_version: int = 0
        self._health_version: int = 0
        # Monotonic sequence counter — every server→client broadcast gets a seq.
        self._seq: int = 0

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    async def register(
        self,
        websocket: WebSocket,
        *,
        endpoint: str = "/ws",
        remote: str = "",
    ) -> str:
        """Register *websocket* and return a short ``conn_id``."""
        conn_id = uuid.uuid4().hex[:8]
        async with self._lock:
            self._clients[conn_id] = ConnectedClient(
                conn_id=conn_id,
                websocket=websocket,
                endpoint=endpoint,
                remote=remote,
            )
        logger.debug(
            "ConnectionRegistry: registered conn=%s endpoint=%s remote=%s total=%d",
            conn_id,
            endpoint,
            remote,
            len(self._clients),
        )
        return conn_id

    async def unregister(self, conn_id: str) -> None:
        """Remove the connection identified by *conn_id*."""
        async with self._lock:
            self._clients.pop(conn_id, None)
        logger.debug(
            "ConnectionRegistry: unregistered conn=%s total=%d",
            conn_id,
            len(self._clients),
        )

    # ------------------------------------------------------------------
    # Broadcast
    # ------------------------------------------------------------------

    @property
    def count(self) -> int:
        """Current number of registered connections."""
        return len(self._clients)

    def next_seq(self) -> int:
        """Return the next monotonic sequence number."""
        self._seq += 1
        return self._seq

    async def broadcast(
        self,
        frame: dict[str, Any],
        *,
        exclude: str | None = None,
    ) -> int:
        """Send *frame* as JSON to every connected client.

        Automatically stamps a monotonic ``seq`` field on each outgoing frame
        (a copy is made so the caller's dict is unchanged).

        Parameters
        ----------
        frame:
            JSON-serialisable dict.
        exclude:
            Optional ``conn_id`` to skip (e.g. the sender).

        Returns
        -------
        int
            Number of clients the frame was successfully delivered to.
        """
        # Stamp with sequence number (mutate a copy so caller's dict is unchanged)
        stamped = {**frame, "seq": self.next_seq()}
        async with self._lock:
            clients = list(self._clients.items())

        sent = 0
        for cid, client in clients:
            if cid == exclude:
                continue
            with contextlib.suppress(Exception):
                await client.websocket.send_json(stamped)
                sent += 1
        return sent

    # ------------------------------------------------------------------
    # Agent state broadcast
    # ------------------------------------------------------------------

    async def broadcast_agent_state(self, state: str, *, conn_id: str | None = None) -> int:
        """Broadcast agent running/idle state change to all connected clients."""
        frame: dict[str, Any] = {"type": "agent", "state": state}
        if conn_id is not None:
            frame["conn_id"] = conn_id
        return await self.broadcast(frame)

    # ------------------------------------------------------------------
    # Presence broadcast (versioned)
    # ------------------------------------------------------------------

    async def broadcast_presence(
        self,
        event: str,
        conn_id: str,
        *,
        endpoint: str = "",
        exclude: str | None = None,
    ) -> int:
        """Broadcast a versioned presence event to all connected clients."""
        self._presence_version += 1
        frame: dict[str, Any] = {
            "type": "presence",
            "event": event,
            "conn_id": conn_id,
            "count": self.count,
            "stateVersion": {
                "presence": self._presence_version,
                "health": self._health_version,
            },
        }
        if endpoint:
            frame["endpoint"] = endpoint
        return await self.broadcast(frame, exclude=exclude)

    # ------------------------------------------------------------------
    # Health broadcast
    # ------------------------------------------------------------------

    async def send_health(self, websocket: WebSocket) -> None:
        """Send current health frame to a single newly-connected client."""
        import importlib.metadata as _meta

        try:
            version = _meta.version("obscura")
        except Exception:
            version = "dev"
        self._health_version += 1
        frame = {
            "type": "health",
            "status": "ok",
            "version": version,
            "connections": self.count,
            "stateVersion": {
                "health": self._health_version,
                "presence": self._presence_version,
            },
            "seq": self.next_seq(),
        }
        with contextlib.suppress(Exception):
            await websocket.send_json(frame)

    async def broadcast_health(self) -> None:
        """Broadcast current health to all connected clients."""
        import importlib.metadata as _meta

        try:
            version = _meta.version("obscura")
        except Exception:
            version = "dev"
        self._health_version += 1
        frame = {
            "type": "health",
            "status": "ok",
            "version": version,
            "connections": self.count,
            "stateVersion": {
                "health": self._health_version,
                "presence": self._presence_version,
            },
        }
        await self.broadcast(frame)

    # ------------------------------------------------------------------
    # Snapshot (diagnostics)
    # ------------------------------------------------------------------

    def snapshot(self) -> list[dict[str, str]]:
        """Return a list of connected-client metadata for diagnostics (no lock — best-effort snapshot)."""
        return [
            {"conn_id": c.conn_id, "endpoint": c.endpoint, "remote": c.remote}
            for c in self._clients.values()
        ]

    # ------------------------------------------------------------------
    # Active reply context (platform → agent → platform)
    # ------------------------------------------------------------------

    def set_active_reply(self, fn: ReplyFn | None) -> None:
        """Store the reply callback for the most-recently received platform message."""
        self._active_reply = fn

    def pop_active_reply(self) -> ReplyFn | None:
        """Consume and return the pending platform reply callback (or None)."""
        fn = self._active_reply
        self._active_reply = None
        return fn

    # ------------------------------------------------------------------
    # Channel fanout task
    # ------------------------------------------------------------------

    def start_channel_fanout(self) -> asyncio.Task[None]:
        """Start the background task that fans platform messages out to all clients.

        Subscribes once to ``channel_inject`` and, for each incoming
        :class:`ChannelMessage`, broadcasts an ``{"type": "incoming", ...}``
        frame to every connected WS client.  Stores the message's ``reply_fn``
        as the active reply context.

        Safe to call multiple times — returns the existing task if already running.
        """
        if self._fanout_task is not None and not self._fanout_task.done():
            return self._fanout_task

        async def _run() -> None:
            from obscura.integrations.messaging.channel_inject import (
                subscribe,
                unsubscribe,
            )

            q = subscribe()
            logger.info("ConnectionRegistry: channel fanout started")
            try:
                while True:
                    msg = await q.get()
                    self.set_active_reply(msg.reply_fn)
                    label = msg.display_name or msg.sender_id
                    frame = {
                        "type": "incoming",
                        "platform": msg.platform,
                        "sender": label,
                        "sender_id": msg.sender_id,
                        "text": msg.text,
                    }
                    delivered = await self.broadcast(frame)
                    logger.debug(
                        "ConnectionRegistry: fanout platform=%s sender=%s delivered=%d",
                        msg.platform,
                        label,
                        delivered,
                    )
            except asyncio.CancelledError:
                unsubscribe(q)
                logger.info("ConnectionRegistry: channel fanout stopped")
                raise

        self._fanout_task = asyncio.create_task(_run())
        return self._fanout_task

    def stop_channel_fanout(self) -> None:
        """Cancel the fanout task if running."""
        if self._fanout_task is not None:
            self._fanout_task.cancel()
            self._fanout_task = None


# Process-level singleton
_registry = ConnectionRegistry()


def get_registry() -> ConnectionRegistry:
    """Return the process-level :class:`ConnectionRegistry` singleton."""
    return _registry


__all__ = [
    "ConnectionRegistry",
    "ConnectedClient",
    "get_registry",
    "ReplyFn",
    "PROTOCOL_VERSION",
    "PROTOCOL_MIN_SUPPORTED",
]
