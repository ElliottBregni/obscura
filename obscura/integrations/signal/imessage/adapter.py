"""iMessage adapter implementing the platform-agnostic messaging contract."""

from __future__ import annotations

import hashlib
import logging

from obscura.integrations.imessage.client import IMessageClient
from obscura.integrations.imessage.state import IMessageState
from obscura.integrations.messaging.identity import normalize_identity
from obscura.integrations.messaging.models import PlatformMessage

logger = logging.getLogger(__name__)


class IMessageAdapter:
    """Normalize iMessage polling/sending behind a generic adapter surface."""

    def __init__(self, contacts: list[str], *, account_id: str = "default") -> None:
        self._contacts = contacts
        self._account_id = account_id
        self._client = IMessageClient(contacts)
        self._state = IMessageState()

    async def start(self) -> None:
        await self._client.check_access()
        if self._state.last_rowid == 0:
            self._state.initialize_from_db(self._client.db_path)
        self._state.clamp_to_db_max(self._client.db_path)

    async def poll(self) -> list[PlatformMessage]:
        raw = await self._client.poll_unread(self._state.last_rowid)
        out: list[PlatformMessage] = []
        for msg in raw:
            sender_id = normalize_identity(msg.sender)
            message_id = msg.guid.strip() if msg.guid else ""
            if not message_id:
                message_id = hashlib.sha1(
                    f"{sender_id}|{msg.date.isoformat()}|{msg.text}".encode("utf-8")
                ).hexdigest()

            out.append(
                PlatformMessage(
                    platform="imessage",
                    account_id=self._account_id,
                    channel_id=f"dm:{sender_id}",
                    sender_id=sender_id,
                    recipient_id="me",
                    message_id=message_id,
                    text=msg.text,
                    timestamp=msg.date,
                    metadata={
                        "sender_raw": msg.sender,
                        "sender_target": msg.sender,
                        "rowid": msg.rowid,
                        "guid": msg.guid,
                    },
                )
            )
            self._state.update(msg.rowid)
        if out:
            logger.info("iMessage adapter produced %d normalized message(s)", len(out))
        return out

    async def send(self, recipient: str, text: str) -> bool:
        return await self._client.send_message(recipient, text)
