"""Push notification MessagePlatformAdapter."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from obscura.core.enums.messaging import PushProvider
from obscura.integrations.push.client import PushClient
from obscura.integrations.push.state import PushState

if TYPE_CHECKING:
    from pathlib import Path

    from obscura.integrations.messaging.models import PlatformMessage

logger = logging.getLogger(__name__)
_PLATFORM = "push"


class PushAdapter:
    """Send push notifications via APNs, FCM, or Expo.

    poll() always returns [] — push is outbound only.
    send() delivers a notification to the recipient device token.
    """

    def __init__(
        self,
        contacts: list[str],
        *,
        account_id: str = "default",
        provider: PushProvider | str = PushProvider.EXPO,
        apns_key_id: str | None = None,
        apns_team_id: str | None = None,
        apns_bundle_id: str | None = None,
        apns_key_path: str | None = None,
        fcm_server_key: str | None = None,
        fcm_project_id: str | None = None,
        state_path: Path | None = None,
        default_title: str = "Obscura",
    ) -> None:
        self._account_id = account_id
        self._default_title = default_title
        self._client = PushClient(
            contacts,
            provider=provider,
            apns_key_id=apns_key_id,
            apns_team_id=apns_team_id,
            apns_bundle_id=apns_bundle_id,
            apns_key_path=apns_key_path,
            fcm_server_key=fcm_server_key,
            fcm_project_id=fcm_project_id,
        )
        self._state = PushState(state_path)

    async def start(self) -> None:
        await self._client.check_access()

    async def poll(self) -> list[PlatformMessage]:
        """Push is outbound-only; always returns empty list."""
        return []

    async def send(self, recipient: str, text: str) -> bool:
        """Send push notification to device token `recipient`."""
        receipt = await self._client.send(
            token=recipient,
            title=self._default_title,
            body=text,
        )
        if receipt.success:
            self._state.mark_sent(receipt.notification_id)
        else:
            logger.warning("Push notification failed: %s", receipt.error)
        return receipt.success
