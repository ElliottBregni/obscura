"""IMessageClient -- read and send iMessages via macOS APIs.

Primary read path: SQLite from ~/Library/Messages/chat.db (requires Full Disk Access).
Fallback read path: AppleScript via osascript (slower, Messages.app must be running).
Send path: Always via osascript subprocess.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CHAT_DB_PATH = Path.home() / "Library" / "Messages" / "chat.db"

# Apple epoch offset: seconds between Unix epoch (1970) and Apple epoch (2001)
_APPLE_EPOCH_OFFSET = 978307200


@dataclass(frozen=True)
class IMessage:
    """A single iMessage."""

    rowid: int
    guid: str
    text: str
    sender: str  # phone number or email
    date: datetime
    is_from_me: bool


class IMessageClient:
    """Read and send iMessages on macOS."""

    def __init__(
        self,
        contacts: list[str],
        *,
        db_path: Path | None = None,
    ) -> None:
        self._contacts = contacts
        self._db_path = db_path or CHAT_DB_PATH
        self._use_sqlite = True
        self._use_copy_fallback = False

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
            return True
        except (sqlite3.OperationalError, sqlite3.DatabaseError):
            # Try copy-based fallback: cp chat.db to temp and read from there
            try:
                import shutil
                import tempfile
                tmp = Path(tempfile.gettempdir()) / "obscura_chat.db"
                shutil.copy2(self._db_path, tmp)
                con = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
                con.execute("SELECT 1 FROM message LIMIT 1")
                con.close()
                tmp.unlink(missing_ok=True)
                logger.info(
                    "Direct SQLite blocked but copy fallback works. "
                    "Using copy-based read for %s.",
                    self._db_path,
                )
                self._use_sqlite = False
                self._use_copy_fallback = True
                return True
            except Exception:
                pass

            logger.warning(
                "Cannot read %s -- Full Disk Access not granted. "
                "Grant FDA in System Settings > Privacy & Security > Full Disk Access.",
                self._db_path,
            )
            self._use_sqlite = False
            self._use_copy_fallback = False
            return False

    async def poll_unread(self, since_rowid: int) -> list[IMessage]:
        """Return new messages from configured contacts since *since_rowid*."""
        if self._use_sqlite:
            return await self._poll_sqlite(since_rowid)
        if getattr(self, "_use_copy_fallback", False):
            return await self._poll_sqlite_copy(since_rowid)
        return await self._poll_applescript()

    async def send_message(self, recipient: str, text: str) -> bool:
        """Send an iMessage via AppleScript. Returns True on success."""
        # Escape for AppleScript string literals
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
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                logger.error("osascript send failed: %s", stderr.decode())
                return False
            return True
        except Exception:
            logger.exception("Failed to send iMessage to %s", recipient)
            return False

    # -- SQLite read path ----------------------------------------------------

    async def _poll_sqlite_copy(self, since_rowid: int) -> list[IMessage]:
        """Copy chat.db to temp, then read. Works without Full Disk Access."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._read_sqlite_copy, since_rowid)

    def _read_sqlite_copy(self, since_rowid: int) -> list[IMessage]:
        import shutil
        import tempfile
        tmp = Path(tempfile.gettempdir()) / "obscura_chat.db"
        try:
            shutil.copy2(self._db_path, tmp)
            return self._read_sqlite(since_rowid, db_path=tmp)
        finally:
            tmp.unlink(missing_ok=True)

    async def _poll_sqlite(self, since_rowid: int) -> list[IMessage]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._read_sqlite, since_rowid)

    def _read_sqlite(self, since_rowid: int, db_path: Path | None = None) -> list[IMessage]:
        target = db_path or self._db_path
        con = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        placeholders = ",".join("?" for _ in self._contacts)
        query = f"""
            SELECT m.ROWID, m.guid, m.text, m.is_from_me, m.date,
                   h.id AS handle_id
            FROM message m
            JOIN handle h ON m.handle_id = h.ROWID
            WHERE m.ROWID > ?
              AND m.is_from_me = 0
              AND h.id IN ({placeholders})
              AND m.text IS NOT NULL
            ORDER BY m.ROWID ASC
        """
        rows = con.execute(query, [since_rowid, *self._contacts]).fetchall()
        con.close()

        messages: list[IMessage] = []
        for row in rows:
            dt = _apple_date_to_datetime(row["date"])
            messages.append(
                IMessage(
                    rowid=row["ROWID"],
                    guid=row["guid"],
                    text=row["text"],
                    sender=row["handle_id"],
                    date=dt,
                    is_from_me=bool(row["is_from_me"]),
                )
            )
        return messages

    # -- AppleScript fallback ------------------------------------------------

    async def _poll_applescript(self) -> list[IMessage]:
        """Best-effort read via AppleScript. Limited to recent messages."""
        messages: list[IMessage] = []
        for contact in self._contacts:
            try:
                msgs = await self._read_applescript_contact(contact)
                messages.extend(msgs)
            except Exception:
                logger.exception("AppleScript poll failed for %s", contact)
        return messages

    async def _read_applescript_contact(self, contact: str) -> list[IMessage]:
        """Read recent messages from a single contact via AppleScript."""
        escaped = contact.replace('"', '\\"')
        # Read the last 10 messages (from newest) by iterating backwards
        script = (
            f'tell application "Messages"\n'
            f'  try\n'
            f'    set output to ""\n'
            f'    repeat with c in every chat\n'
            f'      try\n'
            f'        set ps to participants of c\n'
            f'        repeat with p in ps\n'
            f'          if handle of p is "{escaped}" then\n'
            f'            set msgList to messages of c\n'
            f'            set msgCount to count of msgList\n'
            f'            set startIdx to msgCount\n'
            f'            set collected to 0\n'
            f'            repeat while startIdx > 0 and collected < 10\n'
            f'              set m to item startIdx of msgList\n'
            f'              if sender of m is not missing value then\n'
            f'                set output to output & (id of m) & "||" & '
            f'(content of m) & "\\n"\n'
            f'                set collected to collected + 1\n'
            f'              end if\n'
            f'              set startIdx to startIdx - 1\n'
            f'            end repeat\n'
            f'            return output\n'
            f'          end if\n'
            f'        end repeat\n'
            f'      end try\n'
            f'    end repeat\n'
            f'    return output\n'
            f'  on error\n'
            f'    return ""\n'
            f'  end try\n'
            f"end tell"
        )
        proc = await asyncio.create_subprocess_exec(
            "osascript",
            "-e",
            script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode != 0:
            return []

        results: list[IMessage] = []
        for line in stdout.decode().strip().split("\n"):
            if "||" not in line:
                continue
            parts = line.split("||", 1)
            if len(parts) == 2:
                guid, text = parts
                results.append(
                    IMessage(
                        rowid=hash(guid) & 0x7FFFFFFF,
                        guid=guid.strip(),
                        text=text.strip(),
                        sender=contact,
                        date=datetime.now(tz=timezone.utc),
                        is_from_me=False,
                    )
                )
        return results


def _apple_date_to_datetime(apple_ts: Any) -> datetime:
    """Convert Apple timestamp to UTC datetime."""
    if not apple_ts:
        return datetime.fromtimestamp(0, tz=timezone.utc)
    ts = int(apple_ts)
    # Nanosecond format (post-High Sierra)
    if ts > 1_000_000_000_000:
        unix_ts = (ts / 1_000_000_000) + _APPLE_EPOCH_OFFSET
    else:
        unix_ts = ts + _APPLE_EPOCH_OFFSET
    return datetime.fromtimestamp(unix_ts, tz=timezone.utc)
