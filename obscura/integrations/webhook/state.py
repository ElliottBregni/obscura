"""State for webhook channel — tracks delivery IDs."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from obscura.core.paths import resolve_obscura_state_dir

logger = logging.getLogger(__name__)
_STATE_FILENAME = "webhook_state.json"


class WebhookState:
    """Tracks outbound delivery IDs to avoid duplicate sends."""

    def __init__(self, state_path: Path | None = None) -> None:
        if state_path is None:
            state_path = resolve_obscura_state_dir() / _STATE_FILENAME
        self._path = state_path
        self._delivery_ids: set[str] = set()
        self._load()

    @property
    def delivery_ids(self) -> set[str]:
        return self._delivery_ids

    def mark_delivered(self, delivery_id: str) -> None:
        self._delivery_ids.add(delivery_id)
        if len(self._delivery_ids) > 2000:
            for old in list(self._delivery_ids)[:500]:
                self._delivery_ids.discard(old)
        self._save()

    def _load(self) -> None:
        try:
            if self._path.is_file():
                data = json.loads(self._path.read_text(encoding="utf-8"))
                self._delivery_ids = set(data.get("delivery_ids", []))
        except Exception:
            logger.warning("Failed to load webhook state; starting fresh")
            self._delivery_ids = set()

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps({"delivery_ids": list(self._delivery_ids)}, indent=2),
                encoding="utf-8",
            )
        except Exception:
            logger.warning("Failed to save webhook state to %s", self._path)
