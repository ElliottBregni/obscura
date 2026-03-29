from pathlib import Path

base = Path("/Users/elliottbregni/dev/obscura-main/obscura/integrations/slack")
base.mkdir(parents=True, exist_ok=True)

files = {}

files["__init__.py"] = '''\
"""Slack integration via Slack Web API."""

from obscura.integrations.slack.adapter import SlackAdapter
from obscura.integrations.slack.client import SlackClient, SlackMessage
from obscura.integrations.slack.state import SlackState

__all__ = ["SlackAdapter", "SlackClient", "SlackMessage", "SlackState"]
'''

files["state.py"] = '''\
"""Persistent polling state for Slack (tracks latest message timestamps per channel)."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from obscura.core.paths import resolve_obscura_state_dir

logger = logging.getLogger(__name__)
_STATE_FILENAME = "slack_state.json"


class SlackState:
    """Tracks per-channel latest timestamps to avoid reprocessing."""

    def __init__(self, state_path: Path | None = None) -> None:
        if state_path is None:
            state_path = resolve_obscura_state_dir() / _STATE_FILENAME
        self._path = state_path
        # channel_id -> latest ts string
        self._latest: dict[str, str] = {}
        self._load()

    def get_latest(self, channel_id: str) -> str | None:
        return self._latest.get(channel_id)

    def update(self, channel_id: str, ts: str) -> None:
        current = self._latest.get(channel_id, "0")
        if ts > current:
            self._latest[channel_id] = ts
            self._save()

    def _load(self) -> None:
        try:
            if self._path.is_file():
                data = json.loads(self._path.read_text(encoding="utf-8"))
                self._latest = dict(data.get("latest", {}))
        except Exception:
            logger.warning("Failed to load Slack state; starting fresh")
            self._latest = {}

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps({"latest": self._latest}, indent=2),
                encoding="utf-8",
            )
        except Exception:
            logger.warning("Failed to save Slack state to %s", self._path)
'''

files["client.py"] = '''\
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
            raise RuntimeError(f"Slack auth_test failed: {resp.get(\'error\', \'unknown\')}")
        return True

    async def poll_channel(self, channel_id: str, oldest: str | None = None) -> list[SlackMessage]:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._poll_channel_sync, channel_id, oldest)

    def _poll_channel_sync(self, channel_id: str, oldest: str | None) -> list[SlackMessage]:
        kwargs: dict[str, Any] = {"channel": channel_id, "limit": 100}
        if oldest:
            kwargs["oldest"] = oldest
        resp = self._get_client().conversations_history(**kwargs)
        if not resp["ok"]:
            logger.warning("Slack conversations.history error for %s: %s", channel_id, resp.get("error"))
            return []
        out: list[SlackMessage] = []
        for m in resp.get("messages", []):
            # Skip bot messages, join/leave events
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
            out.append(SlackMessage(
                ts=ts,
                channel_id=channel_id,
                user_id=user_id,
                text=text,
                thread_ts=thread_ts,
                timestamp=timestamp,
                raw=dict(m),
            ))
        return out

    async def send_message(self, channel: str, text: str, thread_ts: str | None = None) -> bool:
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
'''

files["adapter.py"] = '''\
"""Slack MessagePlatformAdapter implementation."""

from __future__ import annotations

import logging
from pathlib import Path

from obscura.integrations.messaging.models import PlatformMessage
from obscura.integrations.slack.client import SlackClient
from obscura.integrations.slack.state import SlackState

logger = logging.getLogger(__name__)
_PLATFORM = "slack"


class SlackAdapter:
    """Normalize Slack polling/sending behind a generic adapter surface."""

    def __init__(
        self,
        contacts: list[str],  # channel IDs or names to monitor
        *,
        account_id: str = "default",
        bot_token: str | None = None,
        state_path: Path | None = None,
    ) -> None:
        self._channels = contacts
        self._account_id = account_id
        self._client = SlackClient(contacts, bot_token=bot_token)
        self._state = SlackState(state_path)

    async def start(self) -> None:
        await self._client.check_access()

    async def poll(self) -> list[PlatformMessage]:
        out: list[PlatformMessage] = []
        for channel_id in self._channels:
            oldest = self._state.get_latest(channel_id)
            msgs = await self._client.poll_channel(channel_id, oldest=oldest)
            for msg in msgs:
                # Skip if we\'ve already seen this ts
                current_latest = self._state.get_latest(channel_id) or "0"
                if msg.ts <= current_latest:
                    continue
                out.append(PlatformMessage(
                    platform=_PLATFORM,
                    account_id=self._account_id,
                    channel_id=channel_id,
                    sender_id=msg.user_id,
                    recipient_id="me",
                    message_id=f"{channel_id}:{msg.ts}",
                    text=msg.text,
                    timestamp=msg.timestamp,
                    metadata={
                        "ts": msg.ts,
                        "thread_ts": msg.thread_ts,
                        **msg.raw,
                    },
                ))
                self._state.update(channel_id, msg.ts)
        return out

    async def send(self, recipient: str, text: str) -> bool:
        """Send to a Slack channel or user. recipient = channel_id or user_id."""
        return await self._client.send_message(recipient, text)
'''

for name, content in files.items():
    p = base / name
    p.write_text(content, encoding="utf-8")
    print(f"  {p.name}: {p.stat().st_size} bytes")

print("All files created.")
