from __future__ import annotations

import os
from typing import TYPE_CHECKING, cast

from .sqlite_impl import SQLiteStorage
import logging

logger = logging.getLogger(__name__)


if TYPE_CHECKING:
    from .storage import Storage


def _default_db_url() -> str:
    """Resolve the default notify DB URL via the optional config.notify module."""
    try:
        from config.notify import get_notify_db_url  # pyright: ignore[reportMissingImports, reportUnknownVariableType]
    except ImportError:
        # Fallback: a file in the user's home, mirroring the obscura runtime layout.
        logger.debug("suppressed exception in _default_db_url", exc_info=True)
        return f"sqlite:///{os.path.expanduser('~/.obscura/notify.db')}"
    return cast(str, get_notify_db_url())


def create_storage(db_url: str | None = None) -> Storage:
    """Create a Storage implementation based on db_url or NOTIFY_DATABASE_URL env var.

    Defaults to SQLite file at ~/.obscura/notify.db when not provided.
    If db_url starts with 'postgres' it returns PostgresStorage (lazy import).
    """
    resolved = db_url or os.environ.get("NOTIFY_DATABASE_URL") or _default_db_url()

    if resolved.startswith("postgres"):
        # lazy import to avoid requiring asyncpg unless used
        from .postgres_impl import PostgresStorage

        return PostgresStorage(resolved)

    # treat everything else as sqlite
    return SQLiteStorage(resolved)
