"""State for push notification channel."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from obscura.core.paths import resolve_obscura_state_dir

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)
_STATE_FILENAME = "push_state.json"


class PushState:
    """Tracks sent notification IDs."""

    def __init__(self, state_path: Path | None = None) -> None:
        if state_path is None:
            state_path = resolve_obscura_state_dir() / _STATE_FILENAME
        self._path = state_path
        self._sent_ids: set[str] = set()
        self._load()

    @property
    def sent_ids(self) -> set[str]:
        return self._sent_ids

    def mark_sent(self, notification_id: str) -> None:
        self._sent_ids.add(notification_id)
        if len(self._sent_ids) > 5000:
            for old in list(self._sent_ids)[:1000]:
                self._sent_ids.discard(old)
        self._save()

    def _load(self) -> None:
        try:
            if self._path.is_file():
                data = json.loads(self._path.read_text(encoding="utf-8"))
                self._sent_ids = set(data.get("sent_ids", []))
        except Exception:
            logger.warning("Failed to load push state; starting fresh")
            self._sent_ids = set()

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps({"sent_ids": list(self._sent_ids)}, indent=2),
                encoding="utf-8",
            )
        except Exception:
            logger.warning("Failed to save push state to %s", self._path)
