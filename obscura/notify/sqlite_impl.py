from __future__ import annotations

import json
import os
import time
from typing import Any, cast

from .storage import Message

_aiosqlite: Any
try:
    import aiosqlite  # pyright: ignore[reportMissingImports]

    _aiosqlite = aiosqlite
except ImportError:
    _aiosqlite = None

# Maximum retry attempts before moving a message to the dead letter queue.
_MAX_ATTEMPTS = 3


class SQLiteStorage:
    """Simple async SQLite-backed Storage implementation."""

    def __init__(self, db_url: str) -> None:
        # accept db_url like 'sqlite:///path/to/file' or 'sqlite:///:memory:' or plain path
        if db_url.startswith("sqlite:///"):
            self.path = db_url[len("sqlite:///") :]
        elif db_url.startswith("sqlite://"):
            self.path = db_url[len("sqlite://") :]
        else:
            self.path = db_url
        self._conn: Any = None

    def _require_conn(self) -> Any:
        if self._conn is None:
            msg = "SQLiteStorage.setup() must be called before use"
            raise RuntimeError(msg)
        return self._conn

    async def setup(self) -> None:
        if _aiosqlite is None:
            msg = "aiosqlite is required: pip install aiosqlite"
            raise ImportError(msg)
        if self.path and self.path != ":memory":
            parent = os.path.dirname(self.path)
            if parent:
                os.makedirs(parent, exist_ok=True)
        self._conn = await _aiosqlite.connect(self.path or ":memory:")
        conn = self._require_conn()
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS messages(
                id TEXT PRIMARY KEY,
                user_id TEXT,
                channel TEXT,
                payload TEXT,
                status TEXT,
                attempts INTEGER,
                created_at INTEGER,
                idempotency_key TEXT,
                last_error TEXT
            )
            """,
        )
        await conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_idempotency
            ON messages(idempotency_key) WHERE idempotency_key IS NOT NULL
            """,
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS dead_letters(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                original_id TEXT NOT NULL,
                user_id TEXT,
                channel TEXT,
                payload TEXT,
                attempts INTEGER,
                reason TEXT,
                created_at INTEGER
            )
            """,
        )
        await conn.commit()

    async def save_message(self, message: Message) -> str | None:
        conn = self._require_conn()
        # Idempotency: if a message with the same key already exists, return its id.
        if message.idempotency_key is not None:
            cur = await conn.execute(
                "SELECT id FROM messages WHERE idempotency_key=?",
                (message.idempotency_key,),
            )
            row = await cur.fetchone()
            if row is not None:
                return cast(str, row[0])

        payload_json = json.dumps(message.payload)
        await conn.execute(
            "INSERT INTO messages(id,user_id,channel,payload,status,attempts,created_at,idempotency_key,last_error) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                message.id,
                message.user_id,
                message.channel,
                payload_json,
                message.status,
                message.attempts,
                int(time.time()),
                message.idempotency_key,
                message.last_error,
            ),
        )
        await conn.commit()
        return message.id

    async def get_message(self, message_id: str) -> Message | None:
        conn = self._require_conn()
        cur = await conn.execute(
            "SELECT id,user_id,channel,payload,status,attempts,idempotency_key,last_error FROM messages WHERE id=?",
            (message_id,),
        )
        row = await cur.fetchone()
        if not row:
            return None
        payload = cast(dict[str, Any], json.loads(cast(str, row[3])))
        return Message(
            id=cast(str, row[0]),
            user_id=cast(str, row[1]),
            channel=cast(str, row[2]),
            payload=payload,
            status=cast(str, row[4]),
            attempts=cast("int | None", row[5]) or 0,
            idempotency_key=cast("str | None", row[6]),
            last_error=cast("str | None", row[7]),
        )

    async def list_pending(self, limit: int = 100) -> list[Message]:
        conn = self._require_conn()
        cur = await conn.execute(
            "SELECT id,user_id,channel,payload,status,attempts,idempotency_key,last_error FROM messages WHERE status IN ('queued','retry') ORDER BY created_at ASC LIMIT ?",
            (limit,),
        )
        rows = cast("list[tuple[Any, ...]]", await cur.fetchall())
        return [
            Message(
                id=cast(str, r[0]),
                user_id=cast(str, r[1]),
                channel=cast(str, r[2]),
                payload=cast(dict[str, Any], json.loads(cast(str, r[3]))),
                status=cast(str, r[4]),
                attempts=cast("int | None", r[5]) or 0,
                idempotency_key=cast("str | None", r[6]),
                last_error=cast("str | None", r[7]),
            )
            for r in rows
        ]

    async def update_status(
        self,
        message_id: str,
        status: str,
        attempts: int | None = None,
        last_error: str | None = None,
    ) -> None:
        conn = self._require_conn()
        parts = ["status=?"]
        params: list[str | int] = [status]
        if attempts is not None:
            parts.append("attempts=?")
            params.append(attempts)
        if last_error is not None:
            parts.append("last_error=?")
            params.append(last_error)
        params.append(message_id)
        await conn.execute(
            f"UPDATE messages SET {', '.join(parts)} WHERE id=?",
            tuple(params),
        )
        await conn.commit()

        # Dead letter queue: if attempts exceed max, move to dead_letters.
        if attempts is not None and attempts >= _MAX_ATTEMPTS and status == "failed":
            msg = await self.get_message(message_id)
            if msg is not None:
                payload_json = json.dumps(msg.payload)
                await conn.execute(
                    "INSERT INTO dead_letters(original_id,user_id,channel,payload,attempts,reason,created_at) VALUES(?,?,?,?,?,?,?)",
                    (
                        message_id,
                        msg.user_id,
                        msg.channel,
                        payload_json,
                        attempts,
                        last_error or "",
                        int(time.time()),
                    ),
                )
                await conn.execute(
                    "DELETE FROM messages WHERE id=?",
                    (message_id,),
                )
                await conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
