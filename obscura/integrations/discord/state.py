"""Persistent polling state for Discord (tracks latest message IDs per channel)."""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from obscura.core.paths import resolve_obscura_state_dir

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

_STATE_FILENAME = "discord_state.json"


class DiscordState:
    """Tracks per-channel latest message snowflake IDs to avoid reprocessing."""

    def __init__(self, state_path: Path | None = None) -> None:
        if state_path is None:
            state_path = resolve_obscura_state_dir() / _STATE_FILENAME
        self._path = state_path
        self._latest: dict[str, str] = {}
        self._load()

    def get_latest(self, channel_id: str) -> str | None:
        return self._latest.get(channel_id)

    def update(self, channel_id: str, msg_id: str) -> None:
        """Advance the cursor for *channel_id* to *msg_id* if it is newer.

        Discord message IDs are 64-bit unsigned integers ("snowflakes") that
        encode a millisecond timestamp in their upper 42 bits.  Because a
        higher integer always means a later message, simple integer comparison
        is sufficient to establish ordering — no date parsing is required.

        The state file is written to disk only when the cursor actually
        advances, avoiding unnecessary I/O on duplicate deliveries.

        Args:
            channel_id: Discord channel snowflake ID whose cursor to update.
            msg_id: Snowflake ID of the message that was just processed.
        """
        current = self._latest.get(channel_id)
        if current is None or int(msg_id) > int(current):
            self._latest[channel_id] = msg_id
            self._save()

    def _load(self) -> None:
        try:
            if self._path.is_file():
                data = json.loads(self._path.read_text(encoding="utf-8"))
                self._latest = dict(data.get("latest", {}))
        except Exception:
            logger.warning("Failed to load Discord state; starting fresh")
            self._latest = {}

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps({"latest": self._latest}, indent=2), encoding="utf-8"
            )
        except Exception:
            logger.warning("Failed to save Discord state to %s", self._path)
