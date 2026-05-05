"""Factory for the keyword-memory repository.

Picks the right backend implementation based on
:func:`obscura.data.engine.resolve_backend`. Caller-friendly: one
function returns a Protocol-typed repo; never expose the underlying
class.
"""

from __future__ import annotations

import logging

from obscura.data.engine import Backend, DataLayerError, resolve_backend, sqlite_path
from obscura.data.keyword_memory.postgres import PostgresKeywordMemoryRepo
from obscura.data.keyword_memory.protocol import KeywordMemoryRepo
from obscura.data.keyword_memory.sqlite import SqliteKeywordMemoryRepo

logger = logging.getLogger(__name__)


_STORE_NAME = "memories"


def get_keyword_memory_repo() -> KeywordMemoryRepo:
    """Return a fresh repository for the lazy keyword-memory store.

    Caller owns the instance and should call ``.close()`` when done.
    The current implementations are stateless beyond schema-init, so
    ``close`` is cheap, but keeping the contract consistent with future
    backends is worth the discipline.

    Postgres init failures fall back to SQLite **with a warning** —
    matching the engine's general fail-loud-on-bad-config posture but
    keeping a single misconfigured turn from blowing up the REPL.
    """
    backend = resolve_backend()
    if backend is Backend.POSTGRES:
        try:
            return PostgresKeywordMemoryRepo()
        except DataLayerError:
            logger.warning(
                "Postgres keyword-memory init failed; falling back to SQLite",
                exc_info=True,
            )
        except Exception:
            logger.warning(
                "Unexpected error initialising Postgres keyword memory; "
                "falling back to SQLite",
                exc_info=True,
            )
    return SqliteKeywordMemoryRepo()


def keyword_memory_available() -> bool:
    """Quick check used by section builders to decide whether to attempt a load.

    Postgres counts as "attempt it" — actual reachability is verified
    inside the call. SQLite is "attempt it" only when the file exists,
    so the very first session (empty store) doesn't render an empty
    section.
    """
    backend = resolve_backend()
    if backend is Backend.POSTGRES:
        return True
    return sqlite_path(_STORE_NAME).exists()
