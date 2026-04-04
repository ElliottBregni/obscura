from __future__ import annotations

import os
from typing import TYPE_CHECKING

from .sqlite_impl import SQLiteStorage

if TYPE_CHECKING:
    from .storage import Storage


def create_storage(db_url: str | None = None) -> Storage:
    """Create a Storage implementation based on db_url or NOTIFY_DATABASE_URL env var.

    Defaults to SQLite file at ~/.obscura/notify.db when not provided.
    If db_url starts with 'postgres' it returns PostgresStorage (lazy import).
    """
    db_url = db_url or os.environ.get("NOTIFY_DATABASE_URL")
    if not db_url:
        # default to SQLite file
        from config.notify import get_notify_db_url

        db_url = get_notify_db_url()

    if db_url.startswith("postgres"):
        # lazy import to avoid requiring asyncpg unless used
        from .postgres_impl import PostgresStorage

        return PostgresStorage(db_url)

    # treat everything else as sqlite
    return SQLiteStorage(db_url)
