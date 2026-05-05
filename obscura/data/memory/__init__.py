"""obscura.data.memory — key-value memory repository.

Phase 5a of the data-layer migration. Wraps the existing
:class:`obscura.memory.store.MemoryStore` and
:class:`obscura.memory.postgres_memory.PostgreSQLMemoryStore` rather
than moving their ~700 lines of working code; future cleanup can fold
the implementations in once the wrapper has soaked.

Public API:

* :class:`MemoryEntry`, :class:`MemoryKey` — value types (re-exported
  from ``obscura.memory.types``)
* :class:`MemoryStore` — Protocol every backend implements
* :func:`get_memory_store` — factory; selects SQLite or Postgres based
  on env (matches the legacy ``create_memory_store`` selector)

Distinct from :mod:`obscura.data.keyword_memory`: this is a
**key-value** store with TTL/expiry/namespaces; the keyword module is
**full-text-search** over arbitrary content. Separate concerns,
separate stores.
"""

from __future__ import annotations

from obscura.data.memory.factory import (
    get_memory_store as get_memory_store,
)
from obscura.data.memory.protocol import (
    MemoryEntry as MemoryEntry,
)
from obscura.data.memory.protocol import (
    MemoryKey as MemoryKey,
)
from obscura.data.memory.protocol import (
    MemoryStore as MemoryStore,
)
