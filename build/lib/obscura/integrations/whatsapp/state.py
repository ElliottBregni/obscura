"""Persistent polling state for WhatsApp (tracks processed message SIDs)."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from obscura.core.paths import resolve_obscura_state_dir

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

_STATE_FILENAME = "whatsapp_state.json"


class WhatsAppState:
    """Tracks processed Twilio message SIDs to avoid reprocessing."""

    def __init__(self, state_path: Path | None = None) -> None:
        if state_path is None:
            state_path = resolve_obscura_state_dir() / _STATE_FILENAME
        self._path = state_path
        self._seen_sids: set[str] = set()
        self._last_fetch_epoch_s: float = 0.0
        self._load()

    @property
    def seen_sids(self) -> set[str]:
        return self._seen_sids

    @property
    def last_fetch_epoch_s(self) -> float:
        return self._last_fetch_epoch_s

    def mark_seen(self, sid: str) -> None:
        self._seen_sids.add(sid)
        if len(self._seen_sids) > 5000:
            overflow = len(self._seen_sids) - 5000
            for old in list(self._seen_sids)[:overflow]:
                self._seen_sids.discard(old)
        self._save()

    def update_fetch_time(self, epoch_s: float) -> None:
        self._last_fetch_epoch_s = epoch_s
        self._save()

    def _load(self) -> None:
        try:
            if self._path.is_file():
                data = json.loads(self._path.read_text(encoding="utf-8"))
                self._seen_sids = set(data.get("seen_sids", []))
                self._last_fetch_epoch_s = float(data.get("last_fetch_epoch_s", 0.0))
        except Exception:
            logger.warning(
                "Failed to load WhatsApp state from %s; starting fresh",
                self._path,
            )
            self._seen_sids = set()
            self._last_fetch_epoch_s = 0.0

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(
                    {
                        "seen_sids": list(self._seen_sids),
                        "last_fetch_epoch_s": self._last_fetch_epoch_s,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        except Exception:
            logger.warning("Failed to save WhatsApp state to %s", self._path)
