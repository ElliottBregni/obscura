"""obscura.kairos.uds_messaging — Cross-session messaging via Unix Domain Sockets.

Enables peer-to-peer messaging between running obscura sessions on
the same machine. Each session listens on a UDS at
``~/.obscura/sockets/<session_id>.sock`` and can send messages to
other sessions by connecting to their sockets.

Protocol: newline-delimited JSON messages.

Pattern from claude-code's ``UDS_INBOX`` feature flag.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

_SOCKET_DIR = Path.home() / ".obscura" / "sockets"


def _socket_path(session_id: str) -> Path:
    """Return the UDS path for a session."""
    return _SOCKET_DIR / f"{session_id}.sock"


# Session IDs hosted IN THIS PROCESS. Used by discover_peers() to filter
# self-broadcasts — without this filter, push_channel_message's UDS
# fanout sends to every .sock file (including our own) and our own
# UDSInbox._on_peer_message re-injects into the local queue, creating
# a per-inbound amplification storm. Updated by UDSInbox.start/stop.
_LOCAL_SESSION_IDS: set[str] = set()


class UDSInbox:
    """Unix Domain Socket inbox for receiving messages from other sessions.

    Usage::

        inbox = UDSInbox(session_id="abc123")
        await inbox.start(on_message=handle_msg)
        # ... session runs ...
        await inbox.stop()
    """

    def __init__(self, session_id: str) -> None:
        self._session_id = session_id
        self._socket_path = _socket_path(session_id)
        self._server: asyncio.AbstractServer | None = None
        self._on_message: Callable[[dict[str, Any]], None] | None = None
        self._messages: list[dict[str, Any]] = []

    async def start(
        self,
        on_message: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        """Start listening for messages."""
        self._on_message = on_message
        _SOCKET_DIR.mkdir(parents=True, exist_ok=True)
        # Register self so discover_peers() filters us out. Without this,
        # _uds_fanout broadcasts our message back to ourselves → infinite
        # re-injection loop via _on_peer_message → push_channel_message →
        # _uds_fanout → self → ...
        _LOCAL_SESSION_IDS.add(self._session_id)

        # Remove stale socket.
        if self._socket_path.exists():
            self._socket_path.unlink()

        self._server = await asyncio.start_unix_server(
            self._handle_connection,
            path=str(self._socket_path),
        )
        logger.info("UDS inbox listening: %s", self._socket_path)

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Handle an incoming connection."""
        try:
            data = await asyncio.wait_for(reader.readline(), timeout=5.0)
            if data:
                msg = json.loads(data.decode("utf-8").strip())
                msg["received_at"] = time.time()
                self._messages.append(msg)
                if self._on_message is not None:
                    self._on_message(msg)
                # Send ack.
                writer.write(json.dumps({"ok": True}).encode("utf-8") + b"\n")
                await writer.drain()
        except Exception:
            logger.debug("UDS inbox: connection error", exc_info=True)
        finally:
            writer.close()

    async def stop(self) -> None:
        """Stop listening and cleanup."""
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        if self._socket_path.exists():
            self._socket_path.unlink(missing_ok=True)
        _LOCAL_SESSION_IDS.discard(self._session_id)
        logger.info("UDS inbox stopped")

    @property
    def messages(self) -> list[dict[str, Any]]:
        """All received messages."""
        return list(self._messages)

    @property
    def unread_count(self) -> int:
        return len(self._messages)


async def send_message(
    target_session_id: str,
    message: dict[str, Any],
    *,
    timeout: float = 5.0,
) -> bool:
    """Send a message to another session's UDS inbox.

    Returns True if delivered successfully.
    """
    sock_path = _socket_path(target_session_id)
    if not sock_path.exists():
        logger.debug("UDS send: target socket not found: %s", sock_path)
        return False

    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(str(sock_path)),
            timeout=timeout,
        )
        msg_bytes = json.dumps(message).encode("utf-8") + b"\n"
        writer.write(msg_bytes)
        await writer.drain()

        # Wait for ack.
        ack_data = await asyncio.wait_for(reader.readline(), timeout=timeout)
        writer.close()
        if ack_data:
            ack = json.loads(ack_data.decode("utf-8").strip())
            return ack.get("ok", False)
        return False
    except Exception:
        logger.debug("UDS send failed to %s", target_session_id, exc_info=True)
        return False


def discover_peers() -> list[str]:
    """Discover other running sessions by scanning the socket directory.

    Excludes any session_id hosted by the current process (registered in
    :data:`_LOCAL_SESSION_IDS` by ``UDSInbox.start``). Without that filter
    the caller's own UDS broadcasts would loop back into the caller's
    own UDS inbox — a per-message amplification storm we observed in
    production with the WhatsApp pipeline.
    """
    if not _SOCKET_DIR.is_dir():
        return []
    peers: list[str] = []
    for sock_file in _SOCKET_DIR.glob("*.sock"):
        session_id = sock_file.stem
        if session_id in _LOCAL_SESSION_IDS:
            continue
        if sock_file.exists():
            peers.append(session_id)
    return peers
