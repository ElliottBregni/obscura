"""IMessageClient -- deterministic iMessage ingest + send.

Read path: SQLite from ~/Library/Messages/chat.db (requires Full Disk Access).
Send path: AppleScript via osascript.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from obscura.integrations.messaging.identity import normalize_identity

logger = logging.getLogger(__name__)

CHAT_DB_PATH = Path.home() / "Library" / "Messages" / "chat.db"

# Apple epoch offset: seconds between Unix epoch (1970) and Apple epoch (2001)
_APPLE_EPOCH_OFFSET = 978307200
_SEND_TIMEOUT_SECONDS = 12.0


@dataclass(frozen=True)
class IMessage:
    """A single inbound iMessage."""

    rowid: int
    guid: str
    text: str
    sender: str  # phone number or email
    date: datetime
    is_from_me: bool


class IMessageClient:
    """Read via SQLite and send via AppleScript on macOS."""

    def __init__(
        self,
        contacts: list[str],
        *,
        db_path: Path | None = None,
    ) -> None:
        self._contacts = contacts
        self._normalized_contacts = {
            normalize_identity(c) for c in contacts if normalize_identity(c) != "unknown"
        }
        self._db_path = db_path or CHAT_DB_PATH
        self._use_sqlite = True
        self._access_recheck_interval_s = 30.0
        self._warn_interval_s = 60.0
        self._next_access_recheck_at = 0.0
        self._next_warn_at = 0.0

    @property
    def db_path(self) -> Path:
        return self._db_path

    async def check_access(self) -> bool:
        """Test whether we can read chat.db. Returns True if SQLite works."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._check_access_sync)

    def _check_access_sync(self) -> bool:
        try:
            con = sqlite3.connect(f"file:{self._db_path}?mode=ro", uri=True)
            con.execute("SELECT 1 FROM message LIMIT 1")
            con.close()
            self._use_sqlite = True
            return True
        except (sqlite3.OperationalError, sqlite3.DatabaseError):
            logger.error(
                "Cannot read %s -- SQLite access required for reliable ingest. "
                "Grant Full Disk Access in System Settings > Privacy & Security > Full Disk Access.",
                self._db_path,
            )
            self._use_sqlite = False
            return False

    async def poll_unread(self, since_rowid: int) -> list[IMessage]:
        """Return new inbound messages from configured contacts since rowid."""
        if not self._use_sqlite:
            now = time.monotonic()
            if now >= self._next_access_recheck_at:
                self._next_access_recheck_at = now + self._access_recheck_interval_s
                if await self.check_access():
                    logger.info("iMessage SQLite access restored; ingest re-enabled")
                else:
                    if now >= self._next_warn_at:
                        logger.warning(
                            "iMessage ingest disabled: SQLite chat.db access unavailable"
                        )
                        self._next_warn_at = now + self._warn_interval_s
                    return []
            else:
                if now >= self._next_warn_at:
                    logger.warning(
                        "iMessage ingest disabled: SQLite chat.db access unavailable"
                    )
                    self._next_warn_at = now + self._warn_interval_s
                return []
        try:
            return await self._poll_sqlite(since_rowid)
        except (sqlite3.OperationalError, sqlite3.DatabaseError):
            logger.exception(
                "SQLite read failed for %s; disabling ingest until next access check",
                self._db_path,
            )
            self._use_sqlite = False
            return []

    async def send_message(self, recipient: str, text: str) -> bool:
        """Send an iMessage via AppleScript. Returns True on success."""
        escaped_text = text.replace("\\", "\\\\").replace('"', '\\"')
        escaped_recipient = recipient.replace('"', '\\"')
        script = (
            f'tell application "Messages"\n'
            f'  set targetService to 1st account whose service type = iMessage\n'
            f'  set targetBuddy to buddy "{escaped_recipient}" of targetService\n'
            f'  send "{escaped_text}" to targetBuddy\n'
            f"end tell"
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                "osascript",
                "-e",
                script,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, stderr = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=_SEND_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                logger.error(
                    "osascript send timed out after %.1fs to %s",
                    _SEND_TIMEOUT_SECONDS,
                    recipient,
                )
                proc.kill()
                await proc.wait()
                return False
            if proc.returncode != 0:
                logger.error("osascript send failed: %s", stderr.decode())
                return False
            return True
        except Exception:
            logger.exception("Failed to send iMessage to %s", recipient)
            return False

    async def _poll_sqlite(self, since_rowid: int) -> list[IMessage]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._read_sqlite, since_rowid)

    def _read_sqlite(self, since_rowid: int) -> list[IMessage]:
        con = sqlite3.connect(f"file:{self._db_path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        query = """
            SELECT m.ROWID, m.guid, m.text, m.is_from_me, m.date,
                   h.id AS handle_id
            FROM message m
            JOIN handle h ON m.handle_id = h.ROWID
            WHERE m.ROWID > ?
              AND m.is_from_me = 0
              AND m.text IS NOT NULL
            ORDER BY m.ROWID ASC
        """
        rows = con.execute(query, [since_rowid]).fetchall()
        con.close()

        messages: list[IMessage] = []
        for row in rows:
            sender = str(row["handle_id"])
            if self._normalized_contacts and normalize_identity(sender) not in self._normalized_contacts:
                continue
            dt = _apple_date_to_datetime(row["date"])
            messages.append(
                IMessage(
                    rowid=int(row["ROWID"]),
                    guid=str(row["guid"] or ""),
                    text=str(row["text"]),
                    sender=sender,
                    date=dt,
                    is_from_me=bool(row["is_from_me"]),
                )
            )
        return messages


def _apple_date_to_datetime(apple_ts: Any) -> datetime:
    """Convert Apple timestamp to UTC datetime."""
    if not apple_ts:
        return datetime.fromtimestamp(0, tz=timezone.utc)
    ts = int(apple_ts)
    if ts > 1_000_000_000_000:
        unix_ts = (ts / 1_000_000_000) + _APPLE_EPOCH_OFFSET
    else:
        unix_ts = ts + _APPLE_EPOCH_OFFSET
    return datetime.fromtimestamp(unix_ts, tz=timezone.utc)
