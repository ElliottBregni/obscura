from __future__ import annotations

import json
import os
import time

import aiosqlite

from .storage import Message

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
        self._conn: aiosqlite.Connection | None = None

    async def setup(self) -> None:
        if self.path and self.path != ":memory":
            parent = os.path.dirname(self.path)
            if parent:
                os.makedirs(parent, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path or ":memory:")
        await self._conn.execute(
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
        await self._conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_idempotency
            ON messages(idempotency_key) WHERE idempotency_key IS NOT NULL
            """,
        )
        await self._conn.execute(
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
        await self._conn.commit()

    async def save_message(self, message: Message) -> str | None:
        # Idempotency: if a message with the same key already exists, return its id.
        if message.idempotency_key is not None:
            cur = await self._conn.execute(
                "SELECT id FROM messages WHERE idempotency_key=?",
                (message.idempotency_key,),
            )
            row = await cur.fetchone()
            if row is not None:
                return row[0]

        await self._conn.execute(
            "INSERT INTO messages(id,user_id,channel,payload,status,attempts,created_at,idempotency_key,last_error) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                message.id,
                message.user_id,
                message.channel,
                json.dumps(message.payload),
                message.status,
                message.attempts,
                int(time.time()),
                message.idempotency_key,
                message.last_error,
            ),
        )
        await self._conn.commit()
        return message.id

    async def get_message(self, message_id: str) -> Message | None:
        cur = await self._conn.execute(
            "SELECT id,user_id,channel,payload,status,attempts,idempotency_key,last_error FROM messages WHERE id=?",
            (message_id,),
        )
        row = await cur.fetchone()
        if not row:
            return None
        return Message(
            id=row[0],
            user_id=row[1],
            channel=row[2],
            payload=json.loads(row[3]),
            status=row[4],
            attempts=row[5] or 0,
            idempotency_key=row[6],
            last_error=row[7],
        )

    async def list_pending(self, limit: int = 100) -> list[Message]:
        cur = await self._conn.execute(
            "SELECT id,user_id,channel,payload,status,attempts,idempotency_key,last_error FROM messages WHERE status IN ('queued','retry') ORDER BY created_at ASC LIMIT ?",
            (limit,),
        )
        rows = await cur.fetchall()
        return [
            Message(
                id=r[0],
                user_id=r[1],
                channel=r[2],
                payload=json.loads(r[3]),
                status=r[4],
                attempts=r[5] or 0,
                idempotency_key=r[6],
                last_error=r[7],
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
        parts = ["status=?"]
        params: list[str | int] = [status]
        if attempts is not None:
            parts.append("attempts=?")
            params.append(attempts)
        if last_error is not None:
            parts.append("last_error=?")
            params.append(last_error)
        params.append(message_id)
        await self._conn.execute(
            f"UPDATE messages SET {', '.join(parts)} WHERE id=?",
            tuple(params),
        )
        await self._conn.commit()

        # Dead letter queue: if attempts exceed max, move to dead_letters.
        if attempts is not None and attempts >= _MAX_ATTEMPTS and status == "failed":
            msg = await self.get_message(message_id)
            if msg is not None:
                await self._conn.execute(
                    "INSERT INTO dead_letters(original_id,user_id,channel,payload,attempts,reason,created_at) VALUES(?,?,?,?,?,?,?)",
                    (
                        message_id,
                        msg.user_id,
                        msg.channel,
                        json.dumps(msg.payload),
                        attempts,
                        last_error or "",
                        int(time.time()),
                    ),
                )
                await self._conn.execute(
                    "DELETE FROM messages WHERE id=?",
                    (message_id,),
                )
                await self._conn.commit()

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
