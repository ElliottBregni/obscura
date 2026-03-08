"""Adapter contracts for transport-specific message platforms."""

from __future__ import annotations

from typing import Protocol

from obscura.integrations.messaging.models import PlatformMessage


class MessagePlatformAdapter(Protocol):
    """Common interface to ingest and send messages from a platform."""

    async def start(self) -> None:
        """Initialize adapter and load cursor/state."""

    async def poll(self) -> list[PlatformMessage]:
        """Poll or receive new inbound messages."""

    async def send(self, recipient: str, text: str) -> bool:
        """Send a plain-text outbound message."""
