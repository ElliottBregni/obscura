"""Factory for the event repository.

Backend selection (highest priority first):

1. Explicit ``db_path`` argument → SQLite at that path.
2. ``OBSCURA_DB_URL`` resolves to Postgres → :class:`PostgresEventRepo`.
3. ``OBSCURA_PG_HOST`` / ``OBSCURA_PG_PASSWORD`` set → Postgres.
4. Default → SQLite at ``~/.obscura/events.db``.

The explicit-path override exists because legacy callers
(``cli/_repl_loop.py``, ``cli/session.py``) construct the store with a
known SQLite path and we don't want to break that contract during the
migration. Once those callers move to ``get_event_repo()`` without
arguments, the explicit-path mode becomes redundant.
"""

from __future__ import annotations

import logging
from pathlib import Path

from obscura.data.engine import Backend, DataLayerError, resolve_backend
from obscura.data.events.postgres import PostgresEventRepo
from obscura.data.events.protocol import EventRepo
from obscura.data.events.sqlite import SqliteEventRepo

logger = logging.getLogger(__name__)


def _default_sqlite_path() -> Path:
    return Path.home() / ".obscura" / "events.db"


def get_event_repo(db_path: str | Path | None = None) -> EventRepo:
    """Return a :class:`EventRepo` for the configured backend."""
    if db_path is not None:
        return SqliteEventRepo(db_path)
    try:
        backend = resolve_backend()
    except DataLayerError:
        logger.debug("backend resolution failed; falling back to SQLite", exc_info=True)
        return SqliteEventRepo(_default_sqlite_path())
    if backend is Backend.POSTGRES:
        return PostgresEventRepo()
    return SqliteEventRepo(_default_sqlite_path())
