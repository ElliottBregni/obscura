"""Discord integration via Discord REST API v10."""

from __future__ import annotations

from obscura.integrations.discord.adapter import DiscordAdapter
from obscura.integrations.discord.client import (
    DiscordAPIError,
    DiscordClient,
    DiscordMessage,
)
from obscura.integrations.discord.state import DiscordState

__all__ = [
    "DiscordAdapter",
    "DiscordAPIError",
    "DiscordClient",
    "DiscordMessage",
    "DiscordState",
]
