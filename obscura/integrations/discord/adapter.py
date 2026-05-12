"""Discord MessagePlatformAdapter implementation."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from obscura.integrations.discord.client import DiscordClient
from obscura.integrations.discord.state import DiscordState
from obscura.integrations.messaging.models import PlatformMessage

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

_PLATFORM = "discord"


class DiscordAdapter:
    """Normalize Discord polling/sending behind a generic adapter surface.

    This adapter wraps :class:`~obscura.integrations.discord.client.DiscordClient`
    and :class:`~obscura.integrations.discord.state.DiscordState` to provide the
    same interface as :class:`~obscura.integrations.slack.adapter.SlackAdapter`.
    """

    def __init__(
        self,
        contacts: list[str],
        *,
        account_id: str = "default",
        bot_token: str | None = None,
        state_path: Path | None = None,
    ) -> None:
        """Initialise the adapter.

        Args:
            contacts: Discord channel IDs to poll and send to.
            account_id: Logical account identifier surfaced in
                :class:`~obscura.integrations.messaging.models.PlatformMessage`.
            bot_token: Discord bot token.  Falls back to ``DISCORD_BOT_TOKEN``
                when *None*.
            state_path: Override the default on-disk state file location.
        """
        self._channels = contacts
        self._account_id = account_id
        self._client = DiscordClient(contacts, bot_token=bot_token)
        self._state = DiscordState(state_path)

    async def start(self) -> None:
        """Validate credentials by calling the Discord API.

        Raises:
            RuntimeError: If the bot token is absent or invalid.
            DiscordAPIError: If Discord returns a non-2xx status on the
                access-check request.
        """
        await self._client.check_access()

    async def poll(self) -> list[PlatformMessage]:
        """Fetch new messages from all configured channels concurrently.

        Channels are polled in parallel via :func:`asyncio.gather`.  Only
        messages with a snowflake ID strictly greater than the last persisted
        cursor are returned.  The state cursor is flushed once per channel
        after all messages for that channel have been processed.

        Returns:
            A list of :class:`~obscura.integrations.messaging.models.PlatformMessage`
            objects, ordered oldest-first within each channel.
        """
        results = await asyncio.gather(
            *[self._poll_channel(ch) for ch in self._channels],
        )
        return [msg for channel_msgs in results for msg in channel_msgs]

    async def _poll_channel(self, channel_id: str) -> list[PlatformMessage]:
        """Poll a single channel and advance its cursor with one state write."""
        after = self._state.get_latest(channel_id)
        msgs = await self._client.poll_channel(channel_id, after=after)
        out: list[PlatformMessage] = []
        latest_id = after
        for msg in msgs:
            if after is not None and int(msg.id) <= int(after):
                continue
            out.append(
                PlatformMessage(
                    platform=_PLATFORM,
                    account_id=self._account_id,
                    channel_id=channel_id,
                    sender_id=msg.author_id,
                    recipient_id="me",
                    message_id=f"{channel_id}:{msg.id}",
                    text=msg.content,
                    timestamp=msg.timestamp,
                    metadata={
                        "message_id": msg.id,
                        "message_reference_id": msg.message_reference_id,
                        **msg.raw,
                    },
                )
            )
            if latest_id is None or int(msg.id) > int(latest_id):
                latest_id = msg.id
        # One state write per channel instead of one per message.
        if latest_id is not None and latest_id != after:
            self._state.update(channel_id, latest_id)
        return out

    async def send(self, recipient: str, text: str) -> bool:
        """Send *text* to a Discord channel.

        Args:
            recipient: Discord channel snowflake ID.
            text: Message content to post.

        Returns:
            ``True`` if delivery succeeded, ``False`` otherwise.
        """
        return await self._client.send_message(recipient, text)

    async def close(self) -> None:
        """Release the underlying HTTP session and free resources."""
        await self._client.close()

    async def send_reply(
        self,
        channel_id: str,
        text: str,
        *,
        reply_to_id: str,
    ) -> bool:
        """Send *text* as a reply to an existing message.

        Args:
            channel_id: Discord channel snowflake ID to post into.
            text: Message content to send.
            reply_to_id: Snowflake ID of the message being replied to.

        Returns:
            ``True`` if delivery succeeded, ``False`` otherwise.
        """
        return await self._client.send_message(
            channel_id, text, message_reference_id=reply_to_id
        )
