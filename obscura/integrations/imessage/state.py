"""Persistent state for iMessage polling -- tracks last-seen ROWID."""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

from obscura.core.paths import resolve_obscura_home

logger = logging.getLogger(__name__)

_STATE_FILENAME = "imessage_state.json"


class IMessageState:
    """Tracks the last-seen message ROWID to avoid reprocessing."""

    def __init__(self, state_path: Path | None = None) -> None:
        self._path = state_path or (resolve_obscura_home() / _STATE_FILENAME)
        self._last_rowid: int = 0
        self._load()

    @property
    def last_rowid(self) -> int:
        return self._last_rowid

    def update(self, rowid: int) -> None:
        """Advance the high-water mark and persist."""
        if rowid > self._last_rowid:
            self._last_rowid = rowid
            self._save()

    def initialize_from_db(self, db_path: Path) -> None:
        """Set last_rowid to current max so we skip existing history."""
        try:
            con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            row = con.execute("SELECT MAX(ROWID) FROM message").fetchone()
            con.close()
            if row and row[0]:
                self._last_rowid = int(row[0])
                self._save()
                logger.debug("Initialized iMessage state to ROWID %d", self._last_rowid)
        except (sqlite3.OperationalError, sqlite3.DatabaseError):
            logger.debug("Could not initialize from chat.db; will set on first poll")

    def clamp_to_db_max(self, db_path: Path) -> None:
        """Clamp state to current max ROWID if persisted state is ahead."""
        try:
            con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            row = con.execute("SELECT MAX(ROWID) FROM message").fetchone()
            con.close()
            max_rowid = int(row[0]) if row and row[0] else 0
            if self._last_rowid > max_rowid:
                logger.warning(
                    "iMessage state ROWID (%d) ahead of DB max (%d); clamping",
                    self._last_rowid,
                    max_rowid,
                )
                self._last_rowid = max_rowid
                self._save()
        except (sqlite3.OperationalError, sqlite3.DatabaseError):
            logger.debug("Could not clamp iMessage state against chat.db")

    def _load(self) -> None:
        if self._path.is_file():
            try:
                data = json.loads(self._path.read_text())
                self._last_rowid = data.get("last_rowid", 0)
            except (json.JSONDecodeError, OSError):
                logger.warning("Could not load %s; starting fresh", self._path)

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps({"last_rowid": self._last_rowid}))
        except OSError:
            logger.exception("Could not save iMessage state to %s", self._path)
