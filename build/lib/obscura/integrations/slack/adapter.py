"""Slack MessagePlatformAdapter implementation."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from obscura.integrations.messaging.models import PlatformMessage
from obscura.integrations.slack.client import SlackClient
from obscura.integrations.slack.state import SlackState

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)
_PLATFORM = "slack"


class SlackAdapter:
    """Normalize Slack polling/sending behind a generic adapter surface."""

    def __init__(
        self,
        contacts: list[str],
        *,
        account_id: str = "default",
        bot_token: str | None = None,
        state_path: Path | None = None,
    ) -> None:
        self._channels = contacts
        self._account_id = account_id
        self._client = SlackClient(contacts, bot_token=bot_token)
        self._state = SlackState(state_path)

    async def start(self) -> None:
        await self._client.check_access()

    async def poll(self) -> list[PlatformMessage]:
        out: list[PlatformMessage] = []
        for channel_id in self._channels:
            oldest = self._state.get_latest(channel_id)
            msgs = await self._client.poll_channel(channel_id, oldest=oldest)
            for msg in msgs:
                current_latest = self._state.get_latest(channel_id) or "0"
                if msg.ts <= current_latest:
                    continue
                out.append(
                    PlatformMessage(
                        platform=_PLATFORM,
                        account_id=self._account_id,
                        channel_id=channel_id,
                        sender_id=msg.user_id,
                        recipient_id="me",
                        message_id=f"{channel_id}:{msg.ts}",
                        text=msg.text,
                        timestamp=msg.timestamp,
                        metadata={"ts": msg.ts, "thread_ts": msg.thread_ts, **msg.raw},
                    ),
                )
                self._state.update(channel_id, msg.ts)
        return out

    async def send(self, recipient: str, text: str) -> bool:
        """Send to a Slack channel or user. recipient = channel_id or user_id."""
        return await self._client.send_message(recipient, text)
