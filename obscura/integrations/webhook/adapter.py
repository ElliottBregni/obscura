"""Generic webhook MessagePlatformAdapter — outbound POST channel."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from obscura.integrations.messaging.models import PlatformMessage
from obscura.integrations.webhook.client import WebhookClient
from obscura.integrations.webhook.state import WebhookState

logger = logging.getLogger(__name__)
_PLATFORM = "webhook"


class WebhookAdapter:
    """Fire-and-forget outbound webhook channel.

    poll() always returns [] — webhooks are outbound only.
    send() POSTs a signed JSON payload to configured URLs.
    """

    def __init__(
        self,
        contacts: list[str],
        *,
        account_id: str = "default",
        secret: str | None = None,
        headers: dict[str, Any] | None = None,
        state_path: Path | None = None,
    ) -> None:
        self._account_id = account_id
        self._client = WebhookClient(
            contacts,
            secret=secret,
            headers={k: str(v) for k, v in (headers or {}).items()},
        )
        self._state = WebhookState(state_path)

    async def start(self) -> None:
        await self._client.check_access()

    async def poll(self) -> list[PlatformMessage]:
        """Webhooks are outbound-only; always returns empty list."""
        return []

    async def send(self, recipient: str, text: str) -> bool:
        delivery = await self._client.deliver(recipient, text)
        if delivery.success:
            self._state.mark_delivered(delivery.delivery_id)
        return delivery.success
