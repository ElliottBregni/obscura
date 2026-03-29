"""Low-level Slack client via slack_sdk Web API."""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SlackMessage:
    """A single inbound Slack message."""

    ts: str
    channel_id: str
    user_id: str
    text: str
    thread_ts: str | None
    timestamp: datetime
    raw: dict[str, Any] = field(default_factory=dict)


class SlackClient:
    """Poll inbound and send outbound Slack messages via slack_sdk."""

    def __init__(
        self,
        channels: list[str],
        *,
        bot_token: str | None = None,
    ) -> None:
        self._channels = channels
        self._token = bot_token or os.environ.get("SLACK_BOT_TOKEN", "")
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                from slack_sdk import WebClient  # type: ignore[import-untyped]

                self._client = WebClient(token=self._token)
            except ImportError as e:
                raise RuntimeError(
                    "slack_sdk is required for Slack integration. "
                    "Install with: pip install slack_sdk"
                ) from e
        return self._client

    async def check_access(self) -> bool:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._check_access_sync)

    def _check_access_sync(self) -> bool:
        if not self._token:
            raise RuntimeError("SLACK_BOT_TOKEN must be set for Slack integration.")
        resp = self._get_client().auth_test()
        if not resp["ok"]:
            raise RuntimeError(f"Slack auth_test failed: {resp.get('error', 'unknown')}")
        return True

    async def poll_channel(
        self, channel_id: str, oldest: str | None = None
    ) -> list[SlackMessage]:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._poll_channel_sync, channel_id, oldest)

    def _poll_channel_sync(self, channel_id: str, oldest: str | None) -> list[SlackMessage]:
        kwargs: dict[str, Any] = {"channel": channel_id, "limit": 100}
        if oldest:
            kwargs["oldest"] = oldest
        resp = self._get_client().conversations_history(**kwargs)
        if not resp["ok"]:
            logger.warning(
                "Slack conversations.history error for %s: %s",
                channel_id,
                resp.get("error"),
            )
            return []
        out: list[SlackMessage] = []
        for m in resp.get("messages", []):
            if m.get("subtype") or m.get("bot_id"):
                continue
            ts = str(m.get("ts", ""))
            user_id = str(m.get("user", ""))
            text = str(m.get("text", ""))
            thread_ts = m.get("thread_ts")
            try:
                epoch = float(ts)
                timestamp = datetime.fromtimestamp(epoch, tz=timezone.utc)
            except (ValueError, TypeError):
                timestamp = datetime.now(tz=timezone.utc)
            out.append(
                SlackMessage(
                    ts=ts,
                    channel_id=channel_id,
                    user_id=user_id,
                    text=text,
                    thread_ts=thread_ts,
                    timestamp=timestamp,
                    raw=dict(m),
                )
            )
        return out

    async def send_message(
        self, channel: str, text: str, thread_ts: str | None = None
    ) -> bool:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._send_sync, channel, text, thread_ts)

    def _send_sync(self, channel: str, text: str, thread_ts: str | None) -> bool:
        try:
            kwargs: dict[str, Any] = {"channel": channel, "text": text}
            if thread_ts:
                kwargs["thread_ts"] = thread_ts
            resp = self._get_client().chat_postMessage(**kwargs)
            return bool(resp.get("ok", False))
        except Exception:
            logger.exception("Slack send failed to %s", channel)
            return False
