"""obscura.data — data access layer.

Repository-pattern scaffolding for SQL/key-value stores. Business code
imports Protocol-typed repos from this package and never touches raw
cursors or connection pools.

Phase 0 (this commit): connection scaffolding only — no domain repos
yet. Phases 1-5 migrate one domain at a time:

  1. keyword memory  (FTS5 / tsvector)
  2. vector memory   (Qdrant default; pgvector / sqlite-vss fallback)
  3. event store + task queue
  4. goal board + session storage
  5. legacy memory/postgres_memory.py + memory/store.py

Conventions, settled:
* Raw SQL organised as ``_QUERIES`` constant dicts per backend
* Backend selection via env (``OBSCURA_DB_URL`` wins; ``OBSCURA_PG_*``
  fallback; SQLite default)
* No SQLAlchemy
* Qdrant is the default vector backend; fail loud when unreachable
"""

from __future__ import annotations

from obscura.data.engine import (
    Backend as Backend,
)
from obscura.data.engine import (
    DataLayerError as DataLayerError,
)
from obscura.data.engine import (
    get_postgres_pool as get_postgres_pool,
)
from obscura.data.engine import (
    postgres_connection as postgres_connection,
)
from obscura.data.engine import (
    resolve_backend as resolve_backend,
)
from obscura.data.engine import (
    sqlite_connection as sqlite_connection,
)
from obscura.data.engine import (
    sqlite_path as sqlite_path,
)
