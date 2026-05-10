"""channel_inject — asyncio queue for injecting platform messages into the REPL."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

_MAX_TEXT_LEN: int = 4_096   # chars — injected text is truncated to this
_MAX_LABEL_LEN: int = 128    # chars — display_name / sender_id prefix cap
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
    """A platform message ready to be injected as user_input."""

    platform: str  # "whatsapp", "imessage", "telegram"
    sender_id: str  # +12316333624, username, etc.
    text: str
    reply_fn: Callable[[str], Awaitable[bool]]  # sends reply back to platform
    display_name: str = ""
    account_id: str = "default"


def _sanitize_label(value: str) -> str:
    """Strip bracket/newline chars that could escape the '[Platform from X]: ' prefix."""
    return value.replace("[", "").replace("]", "").replace("\n", " ").replace("\r", "")[:_MAX_LABEL_LEN]


def _sanitize_text(text: str) -> str:
    """Truncate and strip control characters from injected text."""
    # Remove null bytes
    text = text.replace("\x00", "")
    if len(text) > _MAX_TEXT_LEN:
        text = text[:_MAX_TEXT_LEN] + " [truncated]"
    return text


def get_channel_queue() -> asyncio.Queue[ChannelMessage]:
    return _ensure_queue()


def push_channel_message(msg: ChannelMessage) -> bool:
    """Sanitize and non-blocking push. Returns False if queue is full (message dropped)."""
    # Sanitize in place — create a new dataclass rather than mutate the caller's object
    safe = ChannelMessage(
        platform=msg.platform,
        sender_id=_sanitize_label(msg.sender_id),
        text=_sanitize_text(msg.text),
        reply_fn=msg.reply_fn,
        display_name=_sanitize_label(msg.display_name),
        account_id=msg.account_id,
    )
    try:
        _ensure_queue().put_nowait(safe)
        return True
    except asyncio.QueueFull:
        return False


__all__ = ["ChannelMessage", "get_channel_queue", "push_channel_message"]
