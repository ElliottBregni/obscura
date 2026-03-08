"""SQLite-backed conversation and dedupe state for messaging adapters."""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path

from obscura.core.paths import resolve_obscura_home
from obscura.integrations.messaging.models import ConversationState

logger = logging.getLogger(__name__)

_DB_FILENAME = "messaging_state.db"


class _SQLiteBase:
    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path or (resolve_obscura_home() / _DB_FILENAME)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(self._db_path))
        con.row_factory = sqlite3.Row
        return con

    def _ensure_schema(self) -> None:
        con = self._connect()
        try:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS messaging_conversations (
                    conversation_key TEXT PRIMARY KEY,
                    platform TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    channel_id TEXT NOT NULL,
                    participants_json TEXT NOT NULL,
                    history_json TEXT NOT NULL,
                    last_activity_epoch_s REAL NOT NULL DEFAULT 0,
                    updated_at_epoch_s REAL NOT NULL DEFAULT 0
                )
                """
            )
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS messaging_dedupe (
                    dedupe_id TEXT PRIMARY KEY,
                    seen_at_epoch_s REAL NOT NULL
                )
                """
            )
            con.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_messaging_dedupe_seen_at
                ON messaging_dedupe(seen_at_epoch_s)
                """
            )
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS messaging_send_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at_epoch_s REAL NOT NULL,
                    platform TEXT NOT NULL,
                    conversation_key TEXT NOT NULL,
                    recipient TEXT NOT NULL,
                    success INTEGER NOT NULL,
                    error_text TEXT NOT NULL DEFAULT '',
                    reply_preview TEXT NOT NULL DEFAULT ''
                )
                """
            )
            con.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_messaging_send_events_created
                ON messaging_send_events(created_at_epoch_s DESC)
                """
            )
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS messaging_runtime_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at_epoch_s REAL NOT NULL,
                    component TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    platform TEXT NOT NULL DEFAULT '',
                    conversation_key TEXT NOT NULL DEFAULT '',
                    message_id TEXT NOT NULL DEFAULT '',
                    details_json TEXT NOT NULL DEFAULT '{}'
                )
                """
            )
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS messaging_daemon_locks (
                    lock_name TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    acquired_at_epoch_s REAL NOT NULL,
                    heartbeat_at_epoch_s REAL NOT NULL
                )
                """
            )
            con.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_messaging_runtime_events_created
                ON messaging_runtime_events(created_at_epoch_s DESC)
                """
            )
            con.commit()
        finally:
            con.close()


class ConversationStore(_SQLiteBase):
    """Persistent conversation histories with SQLite durability."""

    def get(self, conversation_key: str) -> ConversationState | None:
        con = self._connect()
        try:
            row = con.execute(
                "SELECT * FROM messaging_conversations WHERE conversation_key = ?",
                (conversation_key,),
            ).fetchone()
            if row is None:
                return None
            return self._row_to_state(row)
        finally:
            con.close()

    def ensure(
        self,
        *,
        conversation_key: str,
        platform: str,
        account_id: str,
        channel_id: str,
        participants: list[str],
    ) -> ConversationState:
        now = time.time()
        con = self._connect()
        try:
            con.execute(
                """
                INSERT INTO messaging_conversations (
                    conversation_key, platform, account_id, channel_id,
                    participants_json, history_json, last_activity_epoch_s, updated_at_epoch_s
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(conversation_key) DO NOTHING
                """,
                (
                    conversation_key,
                    platform,
                    account_id,
                    channel_id,
                    json.dumps(participants, ensure_ascii=True),
                    json.dumps([], ensure_ascii=True),
                    0.0,
                    now,
                ),
            )
            con.commit()
        finally:
            con.close()
        state = self.get(conversation_key)
        if state is None:
            raise RuntimeError(f"Failed to ensure conversation {conversation_key}")
        return state

    def reset_if_stale(self, conversation_key: str, timeout_seconds: float) -> bool:
        state = self.get(conversation_key)
        if state is None:
            return False
        now = time.time()
        if state.last_activity_epoch_s <= 0:
            return False
        if (now - state.last_activity_epoch_s) <= timeout_seconds:
            return False

        con = self._connect()
        try:
            con.execute(
                """
                UPDATE messaging_conversations
                SET history_json = ?, last_activity_epoch_s = ?, updated_at_epoch_s = ?
                WHERE conversation_key = ?
                """,
                (json.dumps([], ensure_ascii=True), now, now, conversation_key),
            )
            con.commit()
        finally:
            con.close()
        return True

    def append_user_message(
        self,
        conversation_key: str,
        text: str,
        *,
        max_history_entries: int = 80,
    ) -> ConversationState:
        return self._append_message(
            conversation_key=conversation_key,
            role="user",
            text=text,
            max_history_entries=max_history_entries,
        )

    def append_assistant_message(
        self,
        conversation_key: str,
        text: str,
        *,
        max_history_entries: int = 80,
    ) -> ConversationState:
        return self._append_message(
            conversation_key=conversation_key,
            role="assistant",
            text=text,
            max_history_entries=max_history_entries,
        )

    def set_last_activity(self, conversation_key: str, epoch_s: float) -> None:
        """Testing/helper hook to force activity timestamps."""
        con = self._connect()
        try:
            con.execute(
                """
                UPDATE messaging_conversations
                SET last_activity_epoch_s = ?, updated_at_epoch_s = ?
                WHERE conversation_key = ?
                """,
                (epoch_s, time.time(), conversation_key),
            )
            con.commit()
        finally:
            con.close()

    @staticmethod
    def user_turn_count(state: ConversationState) -> int:
        return sum(1 for item in state.history if item.get("role") == "user")

    def _append_message(
        self,
        *,
        conversation_key: str,
        role: str,
        text: str,
        max_history_entries: int,
    ) -> ConversationState:
        state = self.get(conversation_key)
        if state is None:
            raise KeyError(f"Conversation not found: {conversation_key}")

        history = list(state.history)
        history.append({"role": role, "text": text})
        if len(history) > max_history_entries:
            history = history[-max_history_entries:]

        now = time.time()
        con = self._connect()
        try:
            con.execute(
                """
                UPDATE messaging_conversations
                SET history_json = ?, last_activity_epoch_s = ?, updated_at_epoch_s = ?
                WHERE conversation_key = ?
                """,
                (
                    json.dumps(history, ensure_ascii=True),
                    now,
                    now,
                    conversation_key,
                ),
            )
            con.commit()
        finally:
            con.close()

        updated = self.get(conversation_key)
        if updated is None:
            raise RuntimeError(f"Conversation vanished after append: {conversation_key}")
        return updated

    @staticmethod
    def _row_to_state(row: sqlite3.Row) -> ConversationState:
        participants = []
        history = []
        try:
            participants = json.loads(row["participants_json"])
        except Exception:
            logger.warning("Invalid participants_json for %s", row["conversation_key"])
        try:
            history = json.loads(row["history_json"])
        except Exception:
            logger.warning("Invalid history_json for %s", row["conversation_key"])

        return ConversationState(
            conversation_key=str(row["conversation_key"]),
            platform=str(row["platform"]),
            account_id=str(row["account_id"]),
            channel_id=str(row["channel_id"]),
            participants=[str(p) for p in participants if isinstance(p, str)],
            history=[
                {"role": str(x.get("role", "")), "text": str(x.get("text", ""))}
                for x in history
                if isinstance(x, dict)
            ],
            last_activity_epoch_s=float(row["last_activity_epoch_s"] or 0.0),
        )


class MessageDedupeStore(_SQLiteBase):
    """Persistent seen-message table with bounded retention."""

    def __init__(self, db_path: Path | None = None, *, max_entries: int = 10000) -> None:
        self._max_entries = max_entries
        super().__init__(db_path=db_path)

    def contains(self, dedupe_id: str) -> bool:
        con = self._connect()
        try:
            row = con.execute(
                "SELECT 1 FROM messaging_dedupe WHERE dedupe_id = ?",
                (dedupe_id,),
            ).fetchone()
            return row is not None
        finally:
            con.close()

    def add(self, dedupe_id: str) -> None:
        now = time.time()
        con = self._connect()
        try:
            con.execute(
                """
                INSERT INTO messaging_dedupe(dedupe_id, seen_at_epoch_s)
                VALUES (?, ?)
                ON CONFLICT(dedupe_id) DO UPDATE SET seen_at_epoch_s = excluded.seen_at_epoch_s
                """,
                (dedupe_id, now),
            )
            # Trim oldest entries to keep bounded size.
            con.execute(
                """
                DELETE FROM messaging_dedupe
                WHERE dedupe_id IN (
                    SELECT dedupe_id FROM messaging_dedupe
                    ORDER BY seen_at_epoch_s DESC
                    LIMIT -1 OFFSET ?
                )
                """,
                (self._max_entries,),
            )
            con.commit()
        finally:
            con.close()

    def add_if_absent(self, dedupe_id: str) -> bool:
        """Atomically insert dedupe key; returns True only for first-seen."""
        now = time.time()
        con = self._connect()
        try:
            cur = con.execute(
                """
                INSERT OR IGNORE INTO messaging_dedupe(dedupe_id, seen_at_epoch_s)
                VALUES (?, ?)
                """,
                (dedupe_id, now),
            )
            inserted = cur.rowcount > 0
            if inserted:
                con.execute(
                    """
                    DELETE FROM messaging_dedupe
                    WHERE dedupe_id IN (
                        SELECT dedupe_id FROM messaging_dedupe
                        ORDER BY seen_at_epoch_s DESC
                        LIMIT -1 OFFSET ?
                    )
                    """,
                    (self._max_entries,),
                )
            con.commit()
            return inserted
        finally:
            con.close()


class MessageSendEventStore(_SQLiteBase):
    """Persistent audit trail for outbound send attempts."""

    def add(
        self,
        *,
        platform: str,
        conversation_key: str,
        recipient: str,
        success: bool,
        error_text: str = "",
        reply_text: str = "",
    ) -> None:
        now = time.time()
        preview = reply_text[:500]
        con = self._connect()
        try:
            con.execute(
                """
                INSERT INTO messaging_send_events (
                    created_at_epoch_s, platform, conversation_key, recipient,
                    success, error_text, reply_preview
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now,
                    platform,
                    conversation_key,
                    recipient,
                    1 if success else 0,
                    error_text,
                    preview,
                ),
            )
            con.commit()
        finally:
            con.close()


class MessageRuntimeEventStore(_SQLiteBase):
    """Persistent structured runtime events for daemon observability."""

    def __init__(self, db_path: Path | None = None, *, max_entries: int = 50000) -> None:
        self._max_entries = max_entries
        super().__init__(db_path=db_path)

    def add(
        self,
        *,
        component: str,
        event_type: str,
        platform: str = "",
        conversation_key: str = "",
        message_id: str = "",
        details: dict[str, object] | None = None,
    ) -> None:
        now = time.time()
        payload = json.dumps(details or {}, ensure_ascii=True)
        con = self._connect()
        try:
            con.execute(
                """
                INSERT INTO messaging_runtime_events (
                    created_at_epoch_s, component, event_type, platform,
                    conversation_key, message_id, details_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now,
                    component,
                    event_type,
                    platform,
                    conversation_key,
                    message_id,
                    payload,
                ),
            )
            con.execute(
                """
                DELETE FROM messaging_runtime_events
                WHERE id IN (
                    SELECT id FROM messaging_runtime_events
                    ORDER BY created_at_epoch_s DESC
                    LIMIT -1 OFFSET ?
                )
                """,
                (self._max_entries,),
            )
            con.commit()
        finally:
            con.close()


class DaemonLockStore(_SQLiteBase):
    """SQLite-backed cross-process lock for one active daemon per name."""

    def try_acquire(
        self,
        *,
        lock_name: str,
        owner_id: str,
        stale_after_s: float = 300.0,
    ) -> bool:
        now = time.time()
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                """
                SELECT owner_id, heartbeat_at_epoch_s
                FROM messaging_daemon_locks
                WHERE lock_name = ?
                """,
                (lock_name,),
            ).fetchone()

            if row is None:
                con.execute(
                    """
                    INSERT INTO messaging_daemon_locks (
                        lock_name, owner_id, acquired_at_epoch_s, heartbeat_at_epoch_s
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (lock_name, owner_id, now, now),
                )
                con.commit()
                return True

            current_owner = str(row["owner_id"])
            heartbeat = float(row["heartbeat_at_epoch_s"] or 0.0)
            is_stale = (now - heartbeat) > stale_after_s

            if current_owner == owner_id or is_stale:
                con.execute(
                    """
                    UPDATE messaging_daemon_locks
                    SET owner_id = ?, heartbeat_at_epoch_s = ?
                    WHERE lock_name = ?
                    """,
                    (owner_id, now, lock_name),
                )
                con.commit()
                return True

            con.commit()
            return False
        finally:
            con.close()

    def heartbeat(self, *, lock_name: str, owner_id: str) -> bool:
        now = time.time()
        con = self._connect()
        try:
            cur = con.execute(
                """
                UPDATE messaging_daemon_locks
                SET heartbeat_at_epoch_s = ?
                WHERE lock_name = ? AND owner_id = ?
                """,
                (now, lock_name, owner_id),
            )
            con.commit()
            return cur.rowcount > 0
        finally:
            con.close()

    def release(self, *, lock_name: str, owner_id: str) -> None:
        con = self._connect()
        try:
            con.execute(
                """
                DELETE FROM messaging_daemon_locks
                WHERE lock_name = ? AND owner_id = ?
                """,
                (lock_name, owner_id),
            )
            con.commit()
        finally:
            con.close()
