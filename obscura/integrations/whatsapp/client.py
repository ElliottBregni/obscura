"""Low-level WhatsApp client via Twilio API."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


def _str_any_dict() -> dict[str, Any]:
    return {}


_POLL_WINDOW_S = 60.0  # seconds of history to fetch on each poll

@dataclass(frozen=True)
class WhatsAppMessage:
    """A single inbound WhatsApp message from Twilio."""

    sid: str
    from_number: str
    to_number: str
    body: str
    date_created: datetime
    status: str
    raw: dict[str, Any] = field(default_factory=_str_any_dict)


class WhatsAppClient:
    """Read inbound and send outbound WhatsApp messages via Twilio."""

    def __init__(
        self,
        contacts: list[str],
        *,
        account_sid: str | None = None,
        auth_token: str | None = None,
        from_number: str | None = None,
    ) -> None:
        self._contacts = [self._normalize_wa(c) for c in contacts]
        self._account_sid = account_sid or os.environ.get("TWILIO_ACCOUNT_SID", "")
        self._auth_token = auth_token or os.environ.get("TWILIO_AUTH_TOKEN", "")
        self._from_number = from_number or os.environ.get("TWILIO_WHATSAPP_FROM", "")
        self._client: Any = None

    @staticmethod
    def _normalize_wa(number: str) -> str:
        n = number.strip()
        if not n.startswith("whatsapp:"):
            n = f"whatsapp:{n}"
        return n

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                from twilio.rest import Client  # type: ignore[import-untyped]

                self._client = Client(self._account_sid, self._auth_token)
            except ImportError as e:
                raise RuntimeError(
                    "twilio package is required for WhatsApp integration. "
                    "Install with: pip install twilio"
                ) from e
        return self._client  # type: ignore[return-value]

    async def check_access(self) -> bool:
        """Verify Twilio credentials are configured."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._check_access_sync)

    def _check_access_sync(self) -> bool:
        if not self._account_sid or not self._auth_token:
            raise RuntimeError(
                "TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN must be set for WhatsApp integration."
            )
        client = self._get_client()
        client.api.accounts(self._account_sid).fetch()
        return True

    async def poll_inbound(self, since_epoch_s: float) -> list[WhatsAppMessage]:
        """Fetch inbound WhatsApp messages since epoch_s."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._poll_inbound_sync, since_epoch_s)

    def _poll_inbound_sync(self, since_epoch_s: float) -> list[WhatsAppMessage]:
        client = self._get_client()
        from_dt = datetime.fromtimestamp(
            max(since_epoch_s, time.time() - _POLL_WINDOW_S), tz=timezone.utc
        )
        messages = client.messages.list(
            to=self._from_number,
            date_sent_after=from_dt,
        )
        out: list[WhatsAppMessage] = []
        for m in messages:
            wa_from = str(m.from_)
            if self._contacts and wa_from not in self._contacts:
                continue
            if getattr(m, "direction", "") not in ("inbound", ""):
                continue
            out.append(
                WhatsAppMessage(
                    sid=str(m.sid),
                    from_number=wa_from,
                    to_number=str(m.to),
                    body=str(m.body or ""),
                    date_created=(
                        m.date_created.replace(tzinfo=timezone.utc)
                        if m.date_created
                        else datetime.now(tz=timezone.utc)
                    ),
                    status=str(m.status),
                    raw={
                        "sid": str(m.sid),
                        "status": str(m.status),
                        "direction": str(getattr(m, "direction", "")),
                    },
                )
            )
        return out

    async def send_message(self, recipient: str, text: str) -> bool:
        """Send a WhatsApp message to recipient."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._send_sync, recipient, text)

    def _send_sync(self, recipient: str, text: str) -> bool:
        try:
            client = self._get_client()
            to = self._normalize_wa(recipient)
            client.messages.create(body=text, from_=self._from_number, to=to)
            return True
        except Exception:
            logger.exception("WhatsApp send failed to %s", recipient)
            return False
