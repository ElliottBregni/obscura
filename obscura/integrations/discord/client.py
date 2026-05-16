"""Low-level Discord client via Discord REST API v10."""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import aiohttp

logger = logging.getLogger(__name__)

_BASE_URL = "https://discord.com/api/v10"
_MAX_RETRIES = 3


def _str_any_dict() -> dict[str, Any]:
    """Return an empty dict used as a dataclass field default factory."""
    return {}


class DiscordAPIError(RuntimeError):
    """Raised when the Discord REST API returns an unexpected error response.

    Attributes:
        status: HTTP status code returned by the API.
        body: Raw response body text for diagnostics.
    """

    def __init__(self, status: int, body: str) -> None:
        """Initialise with the HTTP status and raw response body."""
        super().__init__(f"Discord API error {status}: {body}")
        self.status = status
        self.body = body


@dataclass(frozen=True)
class DiscordMessage:
    """A single inbound Discord message."""

    id: str
    channel_id: str
    author_id: str
    content: str
    timestamp: datetime
    message_reference_id: str | None = None
    raw: dict[str, Any] = field(default_factory=_str_any_dict)


class DiscordClient:
    """Poll inbound and send outbound Discord messages via the REST API v10.

    The client lazily creates an :class:`aiohttp.ClientSession` on first use
    and can be used as an async context manager to ensure prompt cleanup.

    Example::

        async with DiscordClient(["123456789"]) as client:
            await client.check_access()
            messages = await client.poll_channel("123456789")
    """

    def __init__(
        self,
        channels: list[str],
        *,
        bot_token: str | None = None,
    ) -> None:
        """Initialise the client.

        Args:
            channels: Discord channel IDs to poll.
            bot_token: Discord bot token.  Falls back to the
                ``DISCORD_BOT_TOKEN`` environment variable when *None*.
        """
        self._channels = channels
        self._token = bot_token or os.environ.get("DISCORD_BOT_TOKEN") or ""
        self._session: aiohttp.ClientSession | None = None

    # ------------------------------------------------------------------
    # Async context-manager support
    # ------------------------------------------------------------------

    async def __aenter__(self) -> DiscordClient:
        """Enter the async context manager and return *self*."""
        return self

    async def __aexit__(self, *_: object) -> None:
        """Exit the async context manager and close the session."""
        await self.close()

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    async def _get_session(self) -> aiohttp.ClientSession:
        """Return (or lazily create) the shared :class:`aiohttp.ClientSession`.

        Raises:
            RuntimeError: If *aiohttp* is not installed.
        """
        if self._session is None:
            try:
                import aiohttp  # noqa: PLC0415
            except ImportError as exc:
                raise RuntimeError(
                    "aiohttp is required for the Discord integration. "
                    "Install it with: pip install aiohttp"
                ) from exc
            self._session = aiohttp.ClientSession(
                headers={"Authorization": f"Bot {self._token}"}
            )
        return self._session

    async def close(self) -> None:
        """Close the underlying HTTP session and release resources."""
        if self._session is not None:
            await self._session.close()
            self._session = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def check_access(self) -> None:
        """Verify that the bot token is valid by calling ``/users/@me``.

        Raises:
            RuntimeError: If the token is missing.
            DiscordAPIError: If Discord returns a non-2xx status.
        """
        if not self._token:
            raise RuntimeError(
                "Discord bot token is missing. Set DISCORD_BOT_TOKEN or pass bot_token."
            )
        session = await self._get_session()
        async with session.get(f"{_BASE_URL}/users/@me") as resp:
            if not resp.ok:
                text = await resp.text()
                raise DiscordAPIError(resp.status, text)
        logger.debug("Discord access check passed")

    async def poll_channel(
        self,
        channel_id: str,
        after: str | None = None,
    ) -> list[DiscordMessage]:
        """Fetch up to 100 messages from *channel_id* newer than *after*.

        Messages are returned in chronological order (oldest first).  Bot
        messages are silently skipped.  On a 429 rate-limit response the
        method sleeps for the ``Retry-After`` duration and retries up to
        :data:`_MAX_RETRIES` times.

        Args:
            channel_id: Discord channel snowflake ID.
            after: If provided, only messages with a snowflake ID strictly
                greater than *after* are returned.

        Returns:
            A list of :class:`DiscordMessage` objects, oldest first.
        """
        session = await self._get_session()
        params: dict[str, str] = {"limit": "100"}
        if after is not None:
            params["after"] = after

        url = f"{_BASE_URL}/channels/{channel_id}/messages"
        data: list[dict[str, Any]] = []

        for attempt in range(_MAX_RETRIES):
            async with session.get(url, params=params) as resp:
                if resp.status == 429:
                    retry_after = float(resp.headers.get("Retry-After", "1"))
                    logger.warning(
                        "Discord rate-limited on poll_channel %s; "
                        "sleeping %.1fs (attempt %d/%d)",
                        channel_id,
                        retry_after,
                        attempt + 1,
                        _MAX_RETRIES,
                    )
                    await asyncio.sleep(retry_after)
                    continue
                if not resp.ok:
                    text = await resp.text()
                    logger.warning(
                        "Discord poll_channel %s returned %s: %s",
                        channel_id,
                        resp.status,
                        text,
                    )
                    return []
                data = await resp.json()
                break

        messages: list[DiscordMessage] = []
        for raw in data:
            author: dict[str, Any] = raw.get("author") or {}
            if author.get("bot"):
                continue
            ts_str: str = raw.get("timestamp") or ""
            try:
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).astimezone(
                    UTC
                )
            except (ValueError, AttributeError):
                logger.debug("Failed to parse Discord timestamp", exc_info=True)
                ts = datetime.now(tz=UTC)

            ref_id: str | None = None
            ref_msg: dict[str, Any] | None = raw.get("referenced_message")
            if isinstance(ref_msg, dict):
                ref_id = str(ref_msg["id"]) if ref_msg.get("id") else None

            messages.append(
                DiscordMessage(
                    id=str(raw.get("id", "")),
                    channel_id=channel_id,
                    author_id=str(author.get("id", "")),
                    content=str(raw.get("content") or ""),
                    timestamp=ts,
                    message_reference_id=ref_id,
                    raw=raw,
                )
            )

        # Discord returns newest-first; reverse so callers get oldest-first.
        messages.reverse()
        return messages

    async def send_message(
        self,
        channel_id: str,
        text: str,
        *,
        message_reference_id: str | None = None,
    ) -> bool:
        """Post *text* to *channel_id*, optionally as a reply.

        On a 429 rate-limit response the method sleeps for the
        ``Retry-After`` duration and retries up to :data:`_MAX_RETRIES` times.

        Args:
            channel_id: Discord channel snowflake ID to post into.
            text: Message content to send.
            message_reference_id: When provided the message is sent as a
                reply to the message with this snowflake ID.

        Returns:
            ``True`` if the message was delivered successfully, ``False``
            otherwise.
        """
        session = await self._get_session()
        url = f"{_BASE_URL}/channels/{channel_id}/messages"
        payload: dict[str, Any] = {"content": text}
        if message_reference_id is not None:
            payload["message_reference"] = {"message_id": message_reference_id}

        for attempt in range(_MAX_RETRIES):
            async with session.post(url, json=payload) as resp:
                if resp.status == 429:
                    retry_after = float(resp.headers.get("Retry-After", "1"))
                    logger.warning(
                        "Discord rate-limited on send_message to %s; "
                        "sleeping %.1fs (attempt %d/%d)",
                        channel_id,
                        retry_after,
                        attempt + 1,
                        _MAX_RETRIES,
                    )
                    await asyncio.sleep(retry_after)
                    continue
                if not resp.ok:
                    body = await resp.text()
                    logger.warning(
                        "Discord send_message to %s returned %s: %s",
                        channel_id,
                        resp.status,
                        body,
                    )
                    return False
                return True

        logger.warning(
            "Discord send_message to %s failed after %d retries",
            channel_id,
            _MAX_RETRIES,
        )
        return False
