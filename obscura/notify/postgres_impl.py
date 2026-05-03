from __future__ import annotations

import json
import time
from typing import Any, cast

from .storage import Message
import logging

logger = logging.getLogger(__name__)


_asyncpg: Any
try:
    import asyncpg  # pyright: ignore[reportMissingImports]

    _asyncpg = asyncpg
except ImportError:
    logger.debug("suppressed exception in <module>", exc_info=True)
    _asyncpg = None


class PostgresStorage:
    """Async Postgres-backed Storage implementation using asyncpg.

    NOTE: Postgres support is optional. NOTIFY_DATABASE_URL must be a postgres:// DSN.
    """

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self._pool: Any = None

    def _require_pool(self) -> Any:
        if self._pool is None:
            msg = "PostgresStorage.setup() must be called before use"
            raise RuntimeError(msg)
        return self._pool

    async def setup(self) -> None:
        if _asyncpg is None:
            msg = "asyncpg is required: pip install asyncpg"
            raise ImportError(msg)
        self._pool = await _asyncpg.create_pool(dsn=self.dsn)
        pool = self._require_pool()
        async with pool.acquire() as conn:
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
                """,
            )

    async def save_message(self, message: Message) -> str | None:
        pool = self._require_pool()
        payload_json = json.dumps(message.payload)
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO messages(id,user_id,channel,payload,status,attempts,created_at) VALUES($1,$2,$3,$4,$5,$6,$7)",
                message.id,
                message.user_id,
                message.channel,
                payload_json,
                message.status,
                message.attempts,
                int(time.time()),
            )
        return message.id

    async def get_message(self, message_id: str) -> Message | None:
        pool = self._require_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id,user_id,channel,payload,status,attempts FROM messages WHERE id=$1",
                message_id,
            )
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
            )

    async def list_pending(self, limit: int = 100) -> list[Message]:
        pool = self._require_pool()
        async with pool.acquire() as conn:
            rows = cast(
                "list[tuple[Any, ...]]",
                await conn.fetch(
                    "SELECT id,user_id,channel,payload,status,attempts FROM messages WHERE status IN ('queued','retry') ORDER BY created_at ASC LIMIT $1",
                    limit,
                ),
            )
            return [
                Message(
                    id=cast(str, r[0]),
                    user_id=cast(str, r[1]),
                    channel=cast(str, r[2]),
                    payload=cast(dict[str, Any], json.loads(cast(str, r[3]))),
                    status=cast(str, r[4]),
                    attempts=cast("int | None", r[5]) or 0,
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
        pool = self._require_pool()
        async with pool.acquire() as conn:
            if attempts is None and last_error is None:
                await conn.execute(
                    "UPDATE messages SET status=$1 WHERE id=$2",
                    status,
                    message_id,
                )
            elif attempts is not None and last_error is None:
                await conn.execute(
                    "UPDATE messages SET status=$1, attempts=$2 WHERE id=$3",
                    status,
                    attempts,
                    message_id,
                )
            elif attempts is None and last_error is not None:
                await conn.execute(
                    "UPDATE messages SET status=$1, last_error=$2 WHERE id=$3",
                    status,
                    last_error,
                    message_id,
                )
            else:
                await conn.execute(
                    "UPDATE messages SET status=$1, attempts=$2, last_error=$3 WHERE id=$4",
                    status,
                    attempts,
                    last_error,
                    message_id,
                )

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
