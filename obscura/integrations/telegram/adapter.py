"""Telegram MessagePlatformAdapter implementation."""

from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Any, cast

from obscura.integrations.messaging.identity import normalize_identity
from obscura.integrations.messaging.models import PlatformMessage
from obscura.integrations.telegram.client import TelegramClient


logger = logging.getLogger(__name__)

_PLATFORM = "telegram"


def _extract_text(update: dict[str, Any]) -> str | None:
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return None
    return msg.get("text") or msg.get("caption")


def _extract_chat_id(update: dict[str, Any]) -> str | None:
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return None
    chat = msg.get("chat", {})
    return str(chat.get("id", "")) or None


def _extract_sender_id(update: dict[str, Any]) -> str | None:
    msg_any = update.get("message") or update.get("edited_message")
    if not msg_any or not isinstance(msg_any, dict):
        return None
    msg = cast(dict[str, Any], msg_any)
    sender_any = msg.get("from", {})
    sender: dict[str, Any] = (
        cast(dict[str, Any], sender_any) if isinstance(sender_any, dict) else {}
    )
    return str(sender.get("id", "")) or None


def _make_message_id(update: dict[str, Any]) -> str:
    update_id = update.get("update_id", "")
    msg_any: Any = update.get("message")
    msg: dict[str, Any] = (
        cast(dict[str, Any], msg_any) if isinstance(msg_any, dict) else {}
    )
    msg_id = msg.get("message_id", "")
    raw = f"{update_id}:{msg_id}"
    return hashlib.sha1(raw.encode()).hexdigest()


class TelegramAdapter:
    """Webhook + long-poll Telegram Bot adapter implementing MessagePlatformAdapter."""

    def __init__(
        self,
        contacts: list[str],
        *,
        account_id: str = "default",
        bot_token: str | None = None,
        webhook_secret: str | None = None,
    ) -> None:
        self._contacts = set(contacts)  # allowed chat_ids; empty = all
        self._account_id = account_id
        self._webhook_secret = webhook_secret
        self._client = TelegramClient(bot_token)
        self._poll_offset: int | None = None
        self._seen_update_ids: set[int] = set()

    async def start(self) -> None:
        """Verify bot token and log bot identity."""
        me = await self._client.get_me()
        logger.info(
            "Telegram bot connected: @%s (id=%s)",
            me.get("username"),
            me.get("id"),
        )

    async def poll(self) -> list[PlatformMessage]:
        """Long-poll Telegram getUpdates and return normalized messages."""
        try:
            updates = await self._client.get_updates(
                offset=self._poll_offset,
                timeout=10,
            )
        except Exception:
            logger.exception("Telegram poll failed")
            return []

        out: list[PlatformMessage] = []
        for update in updates:
            update_id: int = update.get("update_id", 0)
            if update_id in self._seen_update_ids:
                continue
            self._seen_update_ids.add(update_id)
            self._poll_offset = update_id + 1

            msg = self.normalize_update(update)
            if msg is not None:
                out.append(msg)

        # Bound seen set size
        if len(self._seen_update_ids) > 2000:
            oldest = sorted(self._seen_update_ids)[:-1000]
            self._seen_update_ids -= set(oldest)

        return out

    def normalize_update(self, update: dict[str, Any]) -> PlatformMessage | None:
        """Convert a raw Telegram update dict to a PlatformMessage."""
        text = _extract_text(update)
        if not text:
            return None

        chat_id = _extract_chat_id(update)
        sender_id = _extract_sender_id(update)
        if not chat_id or not sender_id:
            return None

        normalized_sender = normalize_identity(sender_id)

        # Filter to allowed contacts if configured
        if self._contacts and chat_id not in self._contacts:
            logger.debug("Ignoring message from non-allowlisted chat_id=%s", chat_id)
            return None

        return PlatformMessage(
            platform=_PLATFORM,
            account_id=self._account_id,
            channel_id=f"chat:{chat_id}",
            sender_id=normalized_sender,
            recipient_id="bot",
            message_id=_make_message_id(update),
            text=text,
            timestamp=__import__("datetime").datetime.now(
                tz=__import__("datetime").timezone.utc
            ),
            metadata={
                "chat_id": chat_id,
                "update_id": update.get("update_id"),
                "raw_sender": sender_id,
            },
        )

    def verify_signature(
        self, secret_token: str | None, header_value: str | None
    ) -> bool:
        """Verify X-Telegram-Bot-Api-Secret-Token header."""
        if not self._webhook_secret:
            # No secret configured — accept all (dev mode)
            return True
        if not header_value:
            return False
        return hmac.compare_digest(self._webhook_secret, header_value)

    async def send(self, recipient: str, text: str) -> bool:
        """Send a message to a Telegram chat_id."""
        try:
            if len(text) > 4000:
                await self._client.send_long_message(recipient, text)
            else:
                await self._client.send_message(recipient, text)
            return True
        except Exception:
            logger.exception("Telegram send failed to %s", recipient)
            return False

    async def send_typing(self, chat_id: str) -> None:
        """Send typing indicator."""
        try:
            await self._client.send_chat_action(chat_id, "typing")
        except Exception:
            pass  # Non-critical

    async def register_webhook(self, webhook_url: str) -> bool:
        """Register this bot's webhook with Telegram."""
        return await self._client.set_webhook(
            webhook_url,
            secret_token=self._webhook_secret,
        )

    async def remove_webhook(self) -> bool:
        """Remove webhook (switch to polling mode)."""
        return await self._client.delete_webhook()
