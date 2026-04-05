"""Low-level Telegram Bot API HTTP client."""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_TELEGRAM_API_BASE = "https://api.telegram.org"


class TelegramAPIError(RuntimeError):
    """Raised when the Telegram Bot API returns an error."""

    def __init__(self, code: int, description: str) -> None:
        self.code = code
        self.description = description
        super().__init__(f"Telegram API error {code}: {description}")


class TelegramClient:
    """Async Telegram Bot API client for send/receive operations."""

    def __init__(
        self,
        bot_token: str | None = None,
        *,
        timeout_seconds: float = 30.0,
    ) -> None:
        self._token = bot_token or os.environ.get("TELEGRAM_BOT_TOKEN", "")
        if not self._token:
            msg = "Telegram bot token is required. Set TELEGRAM_BOT_TOKEN or pass bot_token."
            raise ValueError(msg)
        self._base = f"{_TELEGRAM_API_BASE}/bot{self._token}"
        self._timeout = httpx.Timeout(timeout_seconds)

    async def _call(self, method: str, **params: Any) -> Any:
        url = f"{self._base}/{method}"
        async with httpx.AsyncClient(timeout=self._timeout) as http:
            resp = await http.post(
                url, json={k: v for k, v in params.items() if v is not None}
            )
        data = resp.json()
        if not data.get("ok"):
            raise TelegramAPIError(
                data.get("error_code", 0),
                data.get("description", "Unknown error"),
            )
        return data["result"]

    async def get_me(self) -> dict[str, Any]:
        """Return bot identity info."""
        result: dict[str, Any] = await self._call("getMe")
        return result

    async def send_message(
        self,
        chat_id: str | int,
        text: str,
        *,
        parse_mode: str | None = "Markdown",
        disable_notification: bool = False,
    ) -> dict[str, Any]:
        """Send a text message to a chat."""
        result: dict[str, Any] = await self._call(
            "sendMessage",
            chat_id=chat_id,
            text=text[:4096],  # Telegram max message length
            parse_mode=parse_mode,
            disable_notification=disable_notification,
        )
        return result

    async def send_long_message(
        self,
        chat_id: str | int,
        text: str,
        *,
        chunk_size: int = 4000,
    ) -> list[dict[str, Any]]:
        """Send a long message split into chunks."""
        results: list[dict[str, Any]] = []
        for i in range(0, len(text), chunk_size):
            chunk = text[i : i + chunk_size]
            result = await self.send_message(chat_id, chunk)
            results.append(result)
        return results

    async def set_webhook(
        self,
        url: str,
        *,
        secret_token: str | None = None,
        allowed_updates: list[str] | None = None,
        max_connections: int = 40,
    ) -> bool:
        """Register a webhook URL with Telegram."""
        result: bool = await self._call(
            "setWebhook",
            url=url,
            secret_token=secret_token,
            allowed_updates=allowed_updates or ["message", "callback_query"],
            max_connections=max_connections,
        )
        return result

    async def delete_webhook(self) -> bool:
        """Remove the registered webhook (fall back to polling)."""
        result: bool = await self._call("deleteWebhook")
        return result

    async def get_updates(
        self,
        offset: int | None = None,
        *,
        limit: int = 100,
        timeout: int = 30,
        allowed_updates: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Long-poll for updates (getUpdates)."""
        result: list[dict[str, Any]] = await self._call(
            "getUpdates",
            offset=offset,
            limit=limit,
            timeout=timeout,
            allowed_updates=allowed_updates or ["message"],
        )
        return result

    async def send_chat_action(
        self,
        chat_id: str | int,
        action: str = "typing",
    ) -> bool:
        """Send a typing indicator."""
        result: bool = await self._call(
            "sendChatAction", chat_id=chat_id, action=action
        )
        return result
