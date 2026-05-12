"""channel_inject — asyncio queue for injecting platform messages into the REPL.

In-process delivery
-------------------
``push_channel_message`` puts the sanitised message into the module-level
``_queue`` (consumed by the REPL loop) and broadcasts a copy to every
``subscribe()`` subscriber (consumed by WebSocket clients in the same
process, e.g. the standalone-agent and ConnectionRegistry fanout task).

Cross-process delivery
----------------------
When an asyncio event loop is running, ``push_channel_message`` also
fire-and-forgets a UDS broadcast so the message reaches any active REPL
sessions running in a *separate* process (e.g. the gateway and REPL are
different processes).  The REPL's ``UDSInbox._on_peer_message`` reconstitutes
the ``ChannelMessage`` and re-injects it into that process's ``_queue``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_MAX_TEXT_LEN: int = 4_096  # chars — injected text is truncated to this
_MAX_LABEL_LEN: int = 128  # chars — display_name / sender_id prefix cap
_QUEUE_MAXSIZE: int = 64

# Module-level singleton — created at import time (thread-safe).
_queue: asyncio.Queue[ChannelMessage]


def _ensure_queue() -> asyncio.Queue[ChannelMessage]:
    global _queue
    try:
        return _queue
    except NameError:
        _queue = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        return _queue


@dataclass
class ChannelMessage:
    """A platform message ready to be injected as user_input.

    ``reply_fn`` sends the final agent response back to the platform —
    rate-limited (the WhatsApp transport enforces a min-gap + hourly cap
    to bound runaway-loop blast radius) and clears the typing indicator
    on completion. Exactly one call expected per turn.

    ``progress_fn`` (optional) sends intermediate "still working" pings
    *while the agent is processing*. It MUST bypass the rate limit (or
    the hourly cap would burn through and prevent the final reply from
    being delivered) and SHOULD NOT clear the typing indicator (the
    keepalive task re-establishes it on its next iteration anyway).
    Set to ``None`` when the channel doesn't support out-of-band pings
    (e.g. peer-injected UDS messages — peers can't send outbound).
    """

    platform: str  # "whatsapp", "imessage", "telegram"
    sender_id: str  # +12316333624, username, etc.
    text: str
    reply_fn: Callable[[str], Awaitable[bool]]  # sends reply back to platform
    progress_fn: Callable[[str], Awaitable[bool]] | None = None
    display_name: str = ""
    account_id: str = "default"


def _sanitize_label(value: str) -> str:
    """Strip bracket/newline chars that could escape the '[Platform from X]: ' prefix."""
    return (
        value.replace("[", "")
        .replace("]", "")
        .replace("\n", " ")
        .replace("\r", "")[:_MAX_LABEL_LEN]
    )


def _sanitize_text(text: str) -> str:
    """Truncate and strip control characters from injected text."""
    # Remove null bytes
    text = text.replace("\x00", "")
    if len(text) > _MAX_TEXT_LEN:
        text = text[:_MAX_TEXT_LEN] + " [truncated]"
    return text


def get_channel_queue() -> asyncio.Queue[ChannelMessage]:
    return _ensure_queue()


# Subscriber list — each entry is an asyncio.Queue that gets a copy of every push
_subscribers: list[asyncio.Queue[ChannelMessage]] = []


def subscribe() -> asyncio.Queue[ChannelMessage]:
    """Create and register a subscriber queue. Returns the queue."""
    q: asyncio.Queue[ChannelMessage] = asyncio.Queue(maxsize=64)
    _subscribers.append(q)
    return q


def unsubscribe(q: asyncio.Queue[ChannelMessage]) -> None:
    """Remove a subscriber queue (call on WebSocket disconnect)."""
    try:
        _subscribers.remove(q)
    except ValueError:
        pass


def push_channel_message(msg: ChannelMessage) -> bool:
    """Sanitize and non-blocking push. Returns False if queue is full (message dropped).

    Delivers to three consumers:

    1. ``_queue`` — the REPL loop in this process polls this queue.
    2. ``_subscribers`` — WebSocket clients (standalone-agent, ConnectionRegistry
       fanout task) each have their own subscriber queue.
    3. UDS broadcast — fire-and-forget task that delivers the message to any
       active REPL sessions running in *separate* processes.  Requires a
       running asyncio event loop; silently skipped otherwise.
    """
    # Sanitize in place — create a new dataclass rather than mutate the caller's object
    safe = ChannelMessage(
        platform=msg.platform,
        sender_id=_sanitize_label(msg.sender_id),
        text=_sanitize_text(msg.text),
        reply_fn=msg.reply_fn,
        progress_fn=msg.progress_fn,
        display_name=_sanitize_label(msg.display_name),
        account_id=msg.account_id,
    )
    try:
        _ensure_queue().put_nowait(safe)
    except asyncio.QueueFull:
        return False

    # Broadcast to all in-process subscribers (non-blocking, drop if full)
    for sub in list(_subscribers):
        try:
            sub.put_nowait(safe)
        except asyncio.QueueFull:
            pass  # subscriber too slow — drop silently

    # Cross-process: fan out to active REPL sessions via UDS
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_uds_fanout(safe))
    except RuntimeError:
        pass  # No running event loop — gateway not started or called from sync context

    return True


async def _uds_fanout(msg: ChannelMessage) -> None:
    """Deliver *msg* to all active REPL sessions via Unix Domain Sockets.

    Fire-and-forget coroutine; all errors are suppressed so a UDS failure
    never affects in-process delivery.  The receiving REPL process's
    ``UDSInbox._on_peer_message`` reconstructs the :class:`ChannelMessage`
    and calls :func:`push_channel_message` there.
    """
    try:
        from obscura.kairos.uds_messaging import discover_peers, send_message

        peers = discover_peers()
        if not peers:
            return

        label = msg.display_name or msg.sender_id
        payload = {
            # Keys consumed by _on_peer_message in composition/blocks/uds_inbox.py
            "platform": msg.platform,
            "sender_id": msg.sender_id,
            "display_name": label,
            "from": label,  # legacy fallback
            "from_session": msg.platform,  # legacy fallback
            "text": msg.text,
            "backend": f"channel:{msg.platform}",
        }
        for session_id in peers:
            await send_message(session_id, payload)
    except Exception:
        logger.debug("channel_inject: UDS fanout failed", exc_info=True)


__all__ = [
    "ChannelMessage",
    "get_channel_queue",
    "push_channel_message",
    "subscribe",
    "unsubscribe",
]
