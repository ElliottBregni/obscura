"""Factory for the vector-memory repository.

Selection rules (highest priority first):

1. ``OBSCURA_VECTOR_MEMORY=off`` → raises :class:`VectorMemoryDisabled`.
2. ``OBSCURA_VECTOR_BACKEND`` env var:
   * ``qdrant`` (default)
   * ``pgvector``
   * ``sqlite-vss``
3. Default → Qdrant (local mode).

**Fail-loud policy.** When the chosen backend can't initialise, this
factory raises :class:`VectorBackendUnavailable` with the underlying
cause. There's no silent fallback — that's how prod misconfig hides.
The keyword-memory factory falls back to SQLite because the file is
always available locally; vector backends have meaningful semantic
differences, so a quiet downgrade would mask bugs.
"""

from __future__ import annotations

import logging
import os

from obscura.data.vector_memory.errors import (
    VectorBackendUnavailable,
    VectorMemoryDisabled,
    VectorMemoryError,
)
from obscura.data.vector_memory.pgvector import PgvectorVectorRepo
from obscura.data.vector_memory.protocol import VectorMemoryRepo
from obscura.data.vector_memory.qdrant import QdrantVectorRepo
from obscura.data.vector_memory.sqlite_vss import SqliteVssVectorRepo

logger = logging.getLogger(__name__)


_VALID_BACKENDS = {"qdrant", "pgvector", "sqlite-vss"}


def is_vector_memory_enabled() -> bool:
    """``True`` unless ``OBSCURA_VECTOR_MEMORY=off`` (or false/0/no)."""
    val = os.environ.get("OBSCURA_VECTOR_MEMORY", "on").strip().lower()
    return val not in ("off", "false", "0", "no")


def resolve_vector_backend() -> str:
    """Return the configured backend name; raise on garbage input."""
    raw = os.environ.get("OBSCURA_VECTOR_BACKEND", "qdrant").strip().lower()
    if raw not in _VALID_BACKENDS:
        msg = (
            f"Unknown OBSCURA_VECTOR_BACKEND={raw!r}. "
            f"Choose one of: {sorted(_VALID_BACKENDS)}"
        )
        raise VectorMemoryError(msg)
    return raw


def get_vector_memory_repo(
    *,
    user_id: str,
    embedding_dim: int,
) -> VectorMemoryRepo:
    """Construct a vector-memory repo for the configured backend.

    Args:
        user_id: Tenant key — used as the collection / table discriminator.
        embedding_dim: Dimensionality of the vectors this repo will store.

    Raises:
        VectorMemoryDisabled: When ``OBSCURA_VECTOR_MEMORY=off``.
        VectorMemoryError: On unknown backend value.
        VectorBackendUnavailable: When the chosen backend can't init.
    """
    if not is_vector_memory_enabled():
        msg = "OBSCURA_VECTOR_MEMORY=off — vector memory is disabled."
        raise VectorMemoryDisabled(msg)

    backend = resolve_vector_backend()
    if backend == "qdrant":
        mode = os.environ.get("OBSCURA_QDRANT_MODE", "local").strip().lower()
        return QdrantVectorRepo(
            user_id=user_id,
            embedding_dim=embedding_dim,
            mode=mode,
            path=os.environ.get("OBSCURA_QDRANT_PATH"),
            url=os.environ.get("QDRANT_URL"),
            api_key=os.environ.get("QDRANT_API_KEY"),
        )
    if backend == "pgvector":
        return PgvectorVectorRepo(user_id=user_id, embedding_dim=embedding_dim)
    if backend == "sqlite-vss":
        return SqliteVssVectorRepo(
            user_id=user_id,
            embedding_dim=embedding_dim,
        )
    # Should be unreachable — resolve_vector_backend() validates.
    msg = f"Backend {backend!r} not wired in factory"
    raise VectorBackendUnavailable(backend, msg)
