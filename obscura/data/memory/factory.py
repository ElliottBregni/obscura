"""Factory for the key-value memory repository.

Mirrors the legacy ``create_memory_store`` selector: Postgres when
``OBSCURA_DB_TYPE=postgresql`` (or the broader data-layer
``OBSCURA_DB_URL`` / ``OBSCURA_PG_*`` config picks Postgres), SQLite
otherwise.

Future cleanup will fold the implementation classes from
``obscura.memory.store`` and ``obscura.memory.postgres_memory`` into
``obscura/data/memory/sqlite.py`` and ``obscura/data/memory/postgres.py``;
for now this is a façade over the existing classes.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from obscura.core.pg_config import is_pg_configured
from obscura.data.engine import Backend, DataLayerError, resolve_backend
from obscura.data.memory.protocol import MemoryStore
from obscura.memory.postgres_memory import PostgreSQLMemoryStore
from obscura.memory.store import MemoryStore as _SqliteMemoryStore

if TYPE_CHECKING:
    from obscura.auth.models import AuthenticatedUser

logger = logging.getLogger(__name__)


def get_memory_store(user: AuthenticatedUser) -> MemoryStore:
    """Return a per-user :class:`MemoryStore` for the configured backend.

    Selection rules (highest priority first):

    1. ``OBSCURA_DB_TYPE=postgresql`` (legacy var) → Postgres.
    2. ``OBSCURA_DB_URL`` resolves to Postgres → Postgres.
    3. ``OBSCURA_PG_HOST`` / ``OBSCURA_PG_PASSWORD`` set → Postgres.
    4. Default → SQLite at ``~/.obscura/memory/<user_hash>.db``.
    """
    if is_pg_configured():
        return PostgreSQLMemoryStore.for_user(user)
    try:
        backend = resolve_backend()
    except DataLayerError:
        logger.debug("backend resolution failed; using SQLite", exc_info=True)
        return _SqliteMemoryStore.for_user(user)
    if backend is Backend.POSTGRES:
        return PostgreSQLMemoryStore.for_user(user)
    return _SqliteMemoryStore.for_user(user)
