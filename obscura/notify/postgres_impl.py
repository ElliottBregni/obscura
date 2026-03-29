from __future__ import annotations
import asyncpg
import json
import time
from typing import List, Optional
from .storage import Storage, Message

class PostgresStorage:
    """Async Postgres-backed Storage implementation using asyncpg.

    NOTE: Postgres support is optional. NOTIFY_DATABASE_URL must be a postgres:// DSN.
    """
    def __init__(self, dsn: str):
        self.dsn = dsn
        self._pool: Optional[asyncpg.pool.Pool] = None

    async def setup(self) -> None:
        self._pool = await asyncpg.create_pool(dsn=self.dsn)
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    user_id TEXT,
                    channel TEXT,
                    payload TEXT,
                    status TEXT,
                    attempts INTEGER,
                    created_at BIGINT
                )
                """
            )

    async def save_message(self, message: Message) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO messages(id,user_id,channel,payload,status,attempts,created_at) VALUES($1,$2,$3,$4,$5,$6,$7)",
                message.id,
                message.user_id,
                message.channel,
                json.dumps(message.payload),
                message.status,
                message.attempts,
                int(time.time()),
            )

    async def get_message(self, message_id: str) -> Optional[Message]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT id,user_id,channel,payload,status,attempts FROM messages WHERE id=$1", message_id)
            if not row:
                return None
            return Message(id=row[0], user_id=row[1], channel=row[2], payload=json.loads(row[3]), status=row[4], attempts=row[5] or 0)

    async def list_pending(self, limit: int = 100) -> List[Message]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("SELECT id,user_id,channel,payload,status,attempts FROM messages WHERE status IN ('queued','retry') ORDER BY created_at ASC LIMIT $1", limit)
            return [Message(id=r[0], user_id=r[1], channel=r[2], payload=json.loads(r[3]), status=r[4], attempts=r[5] or 0) for r in rows]

    async def update_status(self, message_id: str, status: str, attempts: Optional[int] = None) -> None:
        async with self._pool.acquire() as conn:
            if attempts is None:
                await conn.execute("UPDATE messages SET status=$1 WHERE id=$2", status, message_id)
            else:
                await conn.execute("UPDATE messages SET status=$1, attempts=$2 WHERE id=$3", status, attempts, message_id)

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()
