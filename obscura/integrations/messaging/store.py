"""SQLite-backed conversation and dedupe state for messaging adapters."""

from __future__ import annotations

import json
import logging
import sqlite3
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from obscura.core.paths import resolve_obscura_home
from obscura.integrations.messaging.cred_cipher import decrypt_credentials, encrypt_credentials
from obscura.integrations.messaging.models import ConversationState


def _encrypt_creds(creds: dict) -> str:
    """Thin wrapper so callers in this module stay readable."""
    return encrypt_credentials(creds)


def _decrypt_creds(stored: str) -> dict:
    """Thin wrapper so callers in this module stay readable."""
    return decrypt_credentials(stored)

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

_DB_FILENAME = "messaging_state.db"


def _migrate_state_file(old: Path, new: Path) -> None:
    """Move a state file from old to new location if it exists."""
    if old.is_file() and not new.is_file():
        new.parent.mkdir(parents=True, exist_ok=True)
        old.rename(new)
        logging.getLogger(__name__).info("Migrated state file %s -> %s", old, new)


class _SQLiteBase:
    def __init__(self, db_path: Path | None = None) -> None:
        if db_path is None:
            # Backwards-compat: use the legacy OBSCURA_HOME/messaging_state.db
            # location so tests and existing installs find the DB where they
            # expect it.  The previous behaviour moved files into
            # OBSCURA_HOME/state/; keep migration support but prefer the
            # legacy root path for now to reduce surprises.
            new_path = resolve_obscura_home() / _DB_FILENAME
            old_path = resolve_obscura_home() / _DB_FILENAME
            _migrate_state_file(old_path, new_path)
            db_path = new_path
        self._db_path = db_path
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
                """,
            )
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS messaging_dedupe (
                    dedupe_id TEXT PRIMARY KEY,
                    seen_at_epoch_s REAL NOT NULL
                )
                """,
            )
            con.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_messaging_dedupe_seen_at
                ON messaging_dedupe(seen_at_epoch_s)
                """,
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
                """,
            )
            con.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_messaging_send_events_created
                ON messaging_send_events(created_at_epoch_s DESC)
                """,
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
                """,
            )
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS messaging_daemon_locks (
                    lock_name TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    acquired_at_epoch_s REAL NOT NULL,
                    heartbeat_at_epoch_s REAL NOT NULL
                )
                """,
            )
            con.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_messaging_runtime_events_created
                ON messaging_runtime_events(created_at_epoch_s DESC)
                """,
            )
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS messaging_channel_configs (
                    id TEXT PRIMARY KEY,
                    platform TEXT NOT NULL,
                    label TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    mode TEXT NOT NULL DEFAULT 'chat',
                    credentials_json TEXT NOT NULL DEFAULT '{}',
                    router_config_json TEXT NOT NULL DEFAULT '{}',
                    contacts_json TEXT NOT NULL DEFAULT '[]',
                    created_at_epoch_s REAL NOT NULL DEFAULT 0,
                    updated_at_epoch_s REAL NOT NULL DEFAULT 0
                )
                """,
            )
            # Migrate: add mode column if it doesn't exist (existing DBs)
            try:
                con.execute(
                    "ALTER TABLE messaging_channel_configs ADD COLUMN mode TEXT NOT NULL DEFAULT 'chat'"
                )
                con.commit()
            except Exception:
                logger.debug("suppressed exception in _ensure_schema", exc_info=True)
            con.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_messaging_channel_configs_platform
                ON messaging_channel_configs(platform)
                """,
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
            msg = f"Failed to ensure conversation {conversation_key}"
            raise RuntimeError(msg)
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
            msg = f"Conversation not found: {conversation_key}"
            raise KeyError(msg)

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
            msg = f"Conversation vanished after append: {conversation_key}"
            raise RuntimeError(msg)
        return updated

    @staticmethod
    def _row_to_state(row: sqlite3.Row) -> ConversationState:
        participants_raw: list[Any] = []
        history_raw: list[Any] = []
        try:
            loaded_p = json.loads(row["participants_json"])
            if isinstance(loaded_p, list):
                participants_raw = cast(list[Any], loaded_p)
        except Exception:
            logger.warning("Invalid participants_json for %s", row["conversation_key"])
        try:
            loaded_h = json.loads(row["history_json"])
            if isinstance(loaded_h, list):
                history_raw = cast(list[Any], loaded_h)
        except Exception:
            logger.warning("Invalid history_json for %s", row["conversation_key"])

        history_clean: list[dict[str, str]] = []
        for x in history_raw:
            if isinstance(x, dict):
                xd = cast(dict[str, Any], x)
                history_clean.append(
                    {"role": str(xd.get("role", "")), "text": str(xd.get("text", ""))}
                )

        return ConversationState(
            conversation_key=str(row["conversation_key"]),
            platform=str(row["platform"]),
            account_id=str(row["account_id"]),
            channel_id=str(row["channel_id"]),
            participants=[str(p) for p in participants_raw if isinstance(p, str)],
            history=history_clean,
            last_activity_epoch_s=float(row["last_activity_epoch_s"] or 0.0),
        )


class MessageDedupeStore(_SQLiteBase):
    """Persistent seen-message table with bounded retention."""

    def __init__(
        self,
        db_path: Path | None = None,
        *,
        max_entries: int = 10000,
    ) -> None:
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

    def __init__(
        self,
        db_path: Path | None = None,
        *,
        max_entries: int = 50000,
    ) -> None:
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


# ---------------------------------------------------------------------------
# Channel configuration persistence
# ---------------------------------------------------------------------------


@dataclass
class ChannelConfigRecord:
    """Persisted configuration for one registered messaging channel."""

    id: str
    platform: str
    label: str
    enabled: bool
    mode: str  # "chat" or "kairos"
    credentials: dict[str, Any]
    router_config: dict[str, Any]
    contacts: list[str]
    created_at_epoch_s: float
    updated_at_epoch_s: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "platform": self.platform,
            "label": self.label,
            "enabled": self.enabled,
            "mode": self.mode,
            "credentials": self.credentials,
            "router_config": self.router_config,
            "contacts": self.contacts,
            "created_at_epoch_s": self.created_at_epoch_s,
            "updated_at_epoch_s": self.updated_at_epoch_s,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ChannelConfigRecord":
        return cls(
            id=str(data.get("id", "")),
            platform=str(data.get("platform", "")),
            label=str(data.get("label", "")),
            enabled=bool(data.get("enabled", True)),
            mode=str(data.get("mode", "chat")),
            credentials=dict(data.get("credentials", {})),
            router_config=dict(data.get("router_config", {})),
            contacts=list(data.get("contacts", [])),
            created_at_epoch_s=float(data.get("created_at_epoch_s", 0.0)),
            updated_at_epoch_s=float(data.get("updated_at_epoch_s", 0.0)),
        )


class ChannelConfigStore(_SQLiteBase):
    """CRUD store for spec-driven channel configurations."""

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    def create(
        self,
        *,
        platform: str,
        label: str = "",
        enabled: bool = True,
        mode: str = "chat",
        credentials: dict[str, Any] | None = None,
        router_config: dict[str, Any] | None = None,
        contacts: list[str] | None = None,
        config_id: str | None = None,
    ) -> ChannelConfigRecord:
        """Insert a new channel config and return it."""
        now = time.time()
        record_id = config_id or str(uuid.uuid4())
        con = self._connect()
        try:
            con.execute(
                """
                INSERT INTO messaging_channel_configs (
                    id, platform, label, enabled, mode,
                    credentials_json, router_config_json, contacts_json,
                    created_at_epoch_s, updated_at_epoch_s
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record_id,
                    platform.strip().lower(),
                    label,
                    1 if enabled else 0,
                    mode.strip().lower() or "chat",
                    _encrypt_creds(credentials or {}),
                    json.dumps(router_config or {}, ensure_ascii=True),
                    json.dumps(contacts or [], ensure_ascii=True),
                    now,
                    now,
                ),
            )
            con.commit()
        finally:
            con.close()
        rec = self.get(record_id)
        if rec is None:
            msg = f"Failed to create channel config id={record_id}"
            raise RuntimeError(msg)
        logger.info(
            "ChannelConfigStore: created config id=%s platform=%s label=%r",
            record_id,
            platform,
            label,
        )
        return rec

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get(self, config_id: str) -> ChannelConfigRecord | None:
        con = self._connect()
        try:
            row = con.execute(
                "SELECT * FROM messaging_channel_configs WHERE id = ?",
                (config_id,),
            ).fetchone()
            return self._row_to_record(row) if row else None
        finally:
            con.close()

    def list_all(self, *, enabled_only: bool = False) -> list[ChannelConfigRecord]:
        con = self._connect()
        try:
            if enabled_only:
                rows = con.execute(
                    "SELECT * FROM messaging_channel_configs WHERE enabled = 1 "
                    "ORDER BY created_at_epoch_s ASC",
                ).fetchall()
            else:
                rows = con.execute(
                    "SELECT * FROM messaging_channel_configs ORDER BY created_at_epoch_s ASC",
                ).fetchall()
            return [self._row_to_record(r) for r in rows]
        finally:
            con.close()

    def list_by_platform(
        self, platform: str, *, enabled_only: bool = False
    ) -> list[ChannelConfigRecord]:
        con = self._connect()
        try:
            if enabled_only:
                rows = con.execute(
                    "SELECT * FROM messaging_channel_configs "
                    "WHERE platform = ? AND enabled = 1 "
                    "ORDER BY created_at_epoch_s ASC",
                    (platform.strip().lower(),),
                ).fetchall()
            else:
                rows = con.execute(
                    "SELECT * FROM messaging_channel_configs "
                    "WHERE platform = ? ORDER BY created_at_epoch_s ASC",
                    (platform.strip().lower(),),
                ).fetchall()
            return [self._row_to_record(r) for r in rows]
        finally:
            con.close()

    # ------------------------------------------------------------------
    # Update
    # ------------------------------------------------------------------

    def update(
        self,
        config_id: str,
        *,
        label: str | None = None,
        enabled: bool | None = None,
        mode: str | None = None,
        credentials: dict[str, Any] | None = None,
        router_config: dict[str, Any] | None = None,
        contacts: list[str] | None = None,
    ) -> ChannelConfigRecord:
        existing = self.get(config_id)
        if existing is None:
            msg = f"Channel config not found: {config_id}"
            raise KeyError(msg)

        now = time.time()
        new_label = label if label is not None else existing.label
        new_enabled = enabled if enabled is not None else existing.enabled
        new_mode = mode.strip().lower() if mode is not None else existing.mode
        new_creds = credentials if credentials is not None else existing.credentials
        new_rc = router_config if router_config is not None else existing.router_config
        new_contacts = contacts if contacts is not None else existing.contacts

        con = self._connect()
        try:
            con.execute(
                """
                UPDATE messaging_channel_configs
                SET label = ?, enabled = ?, mode = ?,
                    credentials_json = ?, router_config_json = ?,
                    contacts_json = ?, updated_at_epoch_s = ?
                WHERE id = ?
                """,
                (
                    new_label,
                    1 if new_enabled else 0,
                    new_mode,
                    _encrypt_creds(new_creds),
                    json.dumps(new_rc, ensure_ascii=True),
                    json.dumps(new_contacts, ensure_ascii=True),
                    now,
                    config_id,
                ),
            )
            con.commit()
        finally:
            con.close()
        rec = self.get(config_id)
        if rec is None:
            msg = f"Channel config vanished after update: {config_id}"
            raise RuntimeError(msg)
        logger.info("ChannelConfigStore: updated config id=%s", config_id)
        return rec

    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------

    def delete(self, config_id: str) -> bool:
        """Delete a config; returns True if a row was removed."""
        con = self._connect()
        try:
            cur = con.execute(
                "DELETE FROM messaging_channel_configs WHERE id = ?",
                (config_id,),
            )
            con.commit()
            removed = cur.rowcount > 0
        finally:
            con.close()
        if removed:
            logger.info("ChannelConfigStore: deleted config id=%s", config_id)
        return removed

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> ChannelConfigRecord:
        credentials: dict[str, Any] = {}
        router_config: dict[str, Any] = {}
        contacts: list[str] = []
        try:
            credentials = _decrypt_creds(row["credentials_json"] or "{}")
        except Exception:
            logger.debug("suppressed exception in _row_to_record", exc_info=True)
        try:
            router_config = json.loads(row["router_config_json"])
        except Exception:
            logger.debug("suppressed exception in _row_to_record", exc_info=True)
        try:
            contacts = json.loads(row["contacts_json"])
        except Exception:
            logger.debug("suppressed exception in _row_to_record", exc_info=True)
        return ChannelConfigRecord(
            id=str(row["id"]),
            platform=str(row["platform"]),
            label=str(row["label"]),
            enabled=bool(row["enabled"]),
            mode=str(row["mode"] or "chat"),
            credentials=credentials,
            router_config=router_config,
            contacts=[str(c) for c in contacts],
            created_at_epoch_s=float(row["created_at_epoch_s"] or 0.0),
            updated_at_epoch_s=float(row["updated_at_epoch_s"] or 0.0),
        )
