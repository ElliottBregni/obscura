"""channel_inject — asyncio queue for injecting platform messages into the REPL."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field


@dataclass
class ChannelMessage:
    """A platform message ready to be injected as user_input."""

    platform: str  # "whatsapp", "imessage", "telegram"
    sender_id: str  # +12316333624, username, etc.
    text: str
    reply_fn: Callable[[str], Awaitable[bool]]  # sends reply back to platform
    display_name: str = ""
    account_id: str = "default"


# Module-level singleton — created lazily on first access.
_queue: asyncio.Queue[ChannelMessage] | None = None
_QUEUE_MAXSIZE = 64


def get_channel_queue() -> asyncio.Queue[ChannelMessage]:
    global _queue
    if _queue is None:
        _queue = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
    return _queue


def push_channel_message(msg: ChannelMessage) -> bool:
    """Non-blocking push. Returns False if queue is full (message dropped)."""
    try:
        get_channel_queue().put_nowait(msg)
        return True
    except asyncio.QueueFull:
        return False


__all__ = ["ChannelMessage", "get_channel_queue", "push_channel_message"]
