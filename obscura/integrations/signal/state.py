"""Persistent polling state for Signal (tracks last-seen message timestamps)."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from obscura.core.paths import resolve_obscura_state_dir

logger = logging.getLogger(__name__)
_STATE_FILENAME = "signal_state.json"


class SignalState:
    """Tracks the last-seen envelope timestamp per account to avoid reprocessing."""

    def __init__(self, state_path: Path | None = None) -> None:
        if state_path is None:
            state_path = resolve_obscura_state_dir() / _STATE_FILENAME
        self._path = state_path
        self._last_ts_ms: dict[str, int] = {}
        self._load()

    def get_last_ts_ms(self, account: str) -> int:
        return self._last_ts_ms.get(account, 0)

    def update(self, account: str, ts_ms: int) -> None:
        if ts_ms > self._last_ts_ms.get(account, 0):
            self._last_ts_ms[account] = ts_ms
            self._save()

    def _load(self) -> None:
        try:
            if self._path.is_file():
                data = json.loads(self._path.read_text(encoding="utf-8"))
                self._last_ts_ms = {k: int(v) for k, v in data.get("last_ts_ms", {}).items()}
        except Exception:
            logger.warning("Failed to load Signal state; starting fresh")
            self._last_ts_ms = {}

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps({"last_ts_ms": self._last_ts_ms}, indent=2),
                encoding="utf-8",
            )
        except Exception:
            logger.warning("Failed to save Signal state to %s", self._path)
