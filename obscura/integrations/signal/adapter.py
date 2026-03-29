"""Signal MessagePlatformAdapter implementation."""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path

from obscura.integrations.messaging.identity import normalize_identity
from obscura.integrations.messaging.models import PlatformMessage
from obscura.integrations.signal.client import SignalClient
from obscura.integrations.signal.state import SignalState

logger = logging.getLogger(__name__)
_PLATFORM = "signal"


class SignalAdapter:
    """Normalize Signal polling/sending behind a generic adapter surface."""

    def __init__(
        self,
        contacts: list[str],
        *,
        account_id: str = "default",
        account_number: str | None = None,
        base_url: str | None = None,
        state_path: Path | None = None,
    ) -> None:
        self._contacts = contacts
        self._account_id = account_id
        self._client = SignalClient(contacts, account_number=account_number, base_url=base_url)
        self._state = SignalState(state_path)
        self._account_number = account_number or ""

    async def start(self) -> None:
        await self._client.check_access()
        if not self._account_number:
            self._account_number = os.environ.get("SIGNAL_ACCOUNT_NUMBER", "")

    async def poll(self) -> list[PlatformMessage]:
        msgs = await self._client.receive()
        out: list[PlatformMessage] = []
        for msg in msgs:
            last_ts = self._state.get_last_ts_ms(self._account_number)
            if msg.envelope_ts_ms <= last_ts:
                continue
            sender_id = normalize_identity(msg.sender_number)
            channel_id = (
                f"group:{msg.group_id}" if msg.group_id else f"dm:{sender_id}"
            )
            message_id = hashlib.sha1(
                f"{sender_id}|{msg.envelope_ts_ms}|{msg.body}".encode()
            ).hexdigest()
            out.append(
                PlatformMessage(
                    platform=_PLATFORM,
                    account_id=self._account_id,
                    channel_id=channel_id,
                    sender_id=sender_id,
                    recipient_id=normalize_identity(msg.recipient_number),
                    message_id=message_id,
                    text=msg.body,
                    timestamp=msg.timestamp,
                    metadata={
                        "sender_name": msg.sender_name,
                        "group_id": msg.group_id,
                        "envelope_ts_ms": msg.envelope_ts_ms,
                    },
                )
            )
            self._state.update(self._account_number, msg.envelope_ts_ms)
        return out

    async def send(self, recipient: str, text: str) -> bool:
        return await self._client.send_message(recipient, text)
