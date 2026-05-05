"""Factory for the task-queue repository.

Backend selection (highest priority first):

1. ``OBSCURA_DB_URL`` resolves to Postgres → :class:`PostgresTaskRepo`.
2. ``OBSCURA_PG_HOST`` / ``OBSCURA_PG_PASSWORD`` set → Postgres.
3. Default → SQLite at ``~/.obscura/tasks.db``.
"""

from __future__ import annotations

import logging

from obscura.data.engine import Backend, DataLayerError, resolve_backend
from obscura.data.tasks.postgres import PostgresTaskRepo
from obscura.data.tasks.protocol import TaskRepo
from obscura.data.tasks.sqlite import DEFAULT_CLAIM_TIMEOUT, SqliteTaskRepo

logger = logging.getLogger(__name__)


def get_task_repo(claim_timeout: float = DEFAULT_CLAIM_TIMEOUT) -> TaskRepo:
    """Return a :class:`TaskRepo` for the configured backend."""
    try:
        backend = resolve_backend()
    except DataLayerError:
        logger.debug("backend resolution failed; falling back to SQLite", exc_info=True)
        return SqliteTaskRepo(claim_timeout=claim_timeout)
    if backend is Backend.POSTGRES:
        return PostgresTaskRepo(claim_timeout=claim_timeout)
    return SqliteTaskRepo(claim_timeout=claim_timeout)
