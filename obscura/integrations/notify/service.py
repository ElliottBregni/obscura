from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from obscura.notify.factory import create_storage

if TYPE_CHECKING:
    from obscura.notify.storage import Storage

logger = logging.getLogger(__name__)

_storage: Storage | None = None


async def init_storage(db_url: str | None = None) -> Storage:
    global _storage
    if _storage is not None:
        return _storage
    # factory returns a Storage instance (SQLite or Postgres)
    _storage = create_storage(db_url)
    # call setup if async
    try:
        await _storage.setup()
    except Exception as ex:
        logger.exception("Failed to initialize notify storage: %s", ex)
        raise
    return _storage


async def shutdown_storage() -> None:
    global _storage
    if _storage is None:
        return
    try:
        await _storage.close()
    except Exception:
        logger.exception("Error closing notify storage")
    finally:
        _storage = None


def get_storage_sync(db_url: str | None = None) -> Storage:
    """Synchronous accessor for scripts/tests that want a Storage instance without async runtime.

    It will create an event loop to run init_storage once.
    """
    if _storage is not None:
        return _storage
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(init_storage(db_url))
    finally:
        loop.close()
