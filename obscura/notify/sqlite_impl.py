from __future__ import annotations
import aiosqlite
import os
import json
import time
from typing import List, Optional
from .storage import Storage, Message

class SQLiteStorage:
    """Simple async SQLite-backed Storage implementation."""
    def __init__(self, db_url: str):
        # accept db_url like 'sqlite:///path/to/file' or 'sqlite:///:memory:' or plain path
        if db_url.startswith("sqlite:///"):
            self.path = db_url[len("sqlite:///"):]
        elif db_url.startswith("sqlite://"):
            self.path = db_url[len("sqlite://"):]
        else:
            self.path = db_url
        self._conn: Optional[aiosqlite.Connection] = None

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
                created_at INTEGER
            )
            """
        )
        await self._conn.commit()

    async def save_message(self, message: Message) -> None:
        await self._conn.execute(
            "INSERT INTO messages(id,user_id,channel,payload,status,attempts,created_at) VALUES(?,?,?,?,?,?,?)",
            (message.id, message.user_id, message.channel, json.dumps(message.payload), message.status, message.attempts, int(time.time())),
        )
        await self._conn.commit()

    async def get_message(self, message_id: str) -> Optional[Message]:
        cur = await self._conn.execute("SELECT id,user_id,channel,payload,status,attempts FROM messages WHERE id=?", (message_id,))
        row = await cur.fetchone()
        if not row:
            return None
        return Message(id=row[0], user_id=row[1], channel=row[2], payload=json.loads(row[3]), status=row[4], attempts=row[5] or 0)

    async def list_pending(self, limit: int = 100) -> List[Message]:
        cur = await self._conn.execute("SELECT id,user_id,channel,payload,status,attempts FROM messages WHERE status IN ('queued','retry') ORDER BY created_at ASC LIMIT ?", (limit,))
        rows = await cur.fetchall()
        return [Message(id=r[0], user_id=r[1], channel=r[2], payload=json.loads(r[3]), status=r[4], attempts=r[5] or 0) for r in rows]

    async def update_status(self, message_id: str, status: str, attempts: Optional[int] = None) -> None:
        if attempts is None:
            await self._conn.execute("UPDATE messages SET status=? WHERE id=?", (status, message_id))
        else:
            await self._conn.execute("UPDATE messages SET status=?, attempts=? WHERE id=?", (status, attempts, message_id))
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
