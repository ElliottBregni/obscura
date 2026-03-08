"""obscura.integrations.imessage -- macOS iMessage integration."""

from __future__ import annotations

from obscura.integrations.imessage.client import IMessage, IMessageClient
from obscura.integrations.imessage.state import IMessageState

__all__ = ["IMessage", "IMessageClient", "IMessageState"]
