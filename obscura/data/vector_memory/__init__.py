"""obscura.data.vector_memory — vector-store repository.

Phase 2 of the data-layer migration. Hardening layer over the existing
:mod:`obscura.vector_memory.backends` implementations: this package
adds the new Protocol shape, retry/backoff, structured errors, and
healthcheck — without rewriting the proven embedding/search code.

Public API:

* :class:`VectorRecord` — value type returned by search / upsert
* :class:`VectorMemoryRepo` — Protocol every backend implements
* :class:`VectorMemoryError` (and subclasses) — structured failure modes
* :func:`get_vector_memory_repo` — factory; Qdrant by default, fails
  loud when unreachable unless ``OBSCURA_VECTOR_BACKEND`` opts into a
  fallback (``pgvector`` / ``sqlite-vss``)
* :func:`vector_healthcheck` — bool ping for the configured backend

Selection rules:

* ``OBSCURA_VECTOR_BACKEND=qdrant`` (default) | ``pgvector`` | ``sqlite-vss``
* ``OBSCURA_VECTOR_MEMORY=off`` disables vector memory entirely
* For Qdrant local mode: ``OBSCURA_QDRANT_PATH`` (default
  ``~/.obscura/qdrant``); for cloud: ``QDRANT_URL`` + ``QDRANT_API_KEY``

Phase 2c cleanup will fold ``obscura/vector_memory/backends/`` into
this package directly.
"""

from __future__ import annotations

from obscura.data.vector_memory.errors import (
    VectorBackendUnavailable as VectorBackendUnavailable,
)
from obscura.data.vector_memory.errors import (
    VectorMemoryDisabled as VectorMemoryDisabled,
)
from obscura.data.vector_memory.errors import (
    VectorMemoryError as VectorMemoryError,
)
from obscura.data.vector_memory.factory import (
    get_vector_memory_repo as get_vector_memory_repo,
)
from obscura.data.vector_memory.healthcheck import (
    vector_healthcheck as vector_healthcheck,
)
from obscura.data.vector_memory.protocol import (
    VectorMemoryRepo as VectorMemoryRepo,
)
from obscura.data.vector_memory.protocol import (
    VectorRecord as VectorRecord,
)
