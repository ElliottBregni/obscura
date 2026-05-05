"""obscura.data.keyword_memory — lazy keyword-memory repository.

The first domain migrated under the data-layer pattern. Public API:

* :class:`Memory` — value type returned by recall/list operations
* :class:`KeywordMemoryRepo` — Protocol every backend implements
* :func:`get_keyword_memory_repo` — factory that picks SQLite (default)
  or Postgres based on env (``OBSCURA_DB_URL`` / ``OBSCURA_PG_*``)

Backends live in ``sqlite.py`` and ``postgres.py``; raw SQL is
organised as ``_QUERIES`` constant dicts in each, with no business
logic. Connection management is delegated entirely to
:mod:`obscura.data.engine`.
"""

from __future__ import annotations

from obscura.data.keyword_memory.factory import (
    get_keyword_memory_repo as get_keyword_memory_repo,
)
from obscura.data.keyword_memory.factory import (
    keyword_memory_available as keyword_memory_available,
)
from obscura.data.keyword_memory.protocol import (
    KeywordMemoryRepo as KeywordMemoryRepo,
)
from obscura.data.keyword_memory.protocol import (
    Memory as Memory,
)
