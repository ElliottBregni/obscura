"""Persistent polling state for Slack (tracks latest message timestamps per channel)."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from obscura.core.paths import resolve_obscura_state_dir

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)
_STATE_FILENAME = "slack_state.json"


class SlackState:
    """Tracks per-channel latest timestamps to avoid reprocessing."""

    def __init__(self, state_path: Path | None = None) -> None:
        if state_path is None:
            state_path = resolve_obscura_state_dir() / _STATE_FILENAME
        self._path = state_path
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
