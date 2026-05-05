"""WhatsApp MessagePlatformAdapter implementation."""

from __future__ import annotations

import hashlib
import logging
import time
from typing import TYPE_CHECKING

from obscura.integrations.messaging.identity import normalize_identity
from obscura.integrations.messaging.models import PlatformMessage
from obscura.integrations.whatsapp.client import WhatsAppClient
from obscura.integrations.whatsapp.state import WhatsAppState

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

_PLATFORM = "whatsapp"


class WhatsAppAdapter:
    """Normalize WhatsApp polling/sending behind a generic adapter surface."""

    def __init__(
        self,
        contacts: list[str],
        *,
        account_id: str = "default",
        account_sid: str | None = None,
        auth_token: str | None = None,
        from_number: str | None = None,
        state_path: Path | None = None,
    ) -> None:
        self._contacts = contacts
        self._account_id = account_id
        self._client = WhatsAppClient(
            contacts,
            account_sid=account_sid,
            auth_token=auth_token,
            from_number=from_number,
        )
        self._state = WhatsAppState(state_path)

    async def start(self) -> None:
        """Initialize and verify Twilio credentials."""
        await self._client.check_access()
        if self._state.last_fetch_epoch_s == 0.0:
            self._state.update_fetch_time(time.time())

    async def poll(self) -> list[PlatformMessage]:
        """Fetch new inbound WhatsApp messages."""
        since = self._state.last_fetch_epoch_s
        now = time.time()
        raw = await self._client.poll_inbound(since)
        out: list[PlatformMessage] = []
        for msg in raw:
            if msg.sid in self._state.seen_sids:
                continue
            sender_id = normalize_identity(msg.from_number.replace("whatsapp:", ""))
            message_id = (
                msg.sid
                or hashlib.sha1(
                    f"{sender_id}|{msg.date_created.isoformat()}|{msg.body}".encode(),
                ).hexdigest()
            )
            out.append(
                PlatformMessage(
                    platform=_PLATFORM,
                    account_id=self._account_id,
                    channel_id=f"dm:{sender_id}",
                    sender_id=sender_id,
                    recipient_id="me",
                    message_id=message_id,
                    text=msg.body,
                    timestamp=msg.date_created,
                    metadata=msg.raw,
                ),
            )
            self._state.mark_seen(msg.sid)
        self._state.update_fetch_time(now)
        return out

    async def send(self, recipient: str, text: str) -> bool:
        """Send a WhatsApp message."""
        return await self._client.send_message(recipient, text)
