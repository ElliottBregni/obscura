"""sqlite-vss adapter — opt-in fallback for the vector-memory repository.

Selected only when ``OBSCURA_VECTOR_BACKEND=sqlite-vss``. Wraps
:class:`obscura.vector_memory.backends.sqlite_backend.SQLiteBackend`
through the shared :class:`LegacyBackendAdapter`.
"""

from __future__ import annotations

import logging

from obscura.data.vector_memory._legacy_adapter import LegacyBackendAdapter
from obscura.data.vector_memory.errors import VectorBackendUnavailable
from obscura.vector_memory.backends.base import BackendConfig
from obscura.vector_memory.backends.sqlite_backend import SQLiteBackend

logger = logging.getLogger(__name__)


class SqliteVssVectorRepo(LegacyBackendAdapter):
    """SQLite-vss :class:`VectorMemoryRepo` implementation."""

    def __init__(self, *, user_id: str, embedding_dim: int) -> None:
        try:
            backend = SQLiteBackend(
                config=BackendConfig(user_id=user_id, embedding_dim=embedding_dim),
            )
        except Exception as exc:
            raise VectorBackendUnavailable("sqlite-vss", exc) from exc
        super().__init__(backend=backend, name="sqlite-vss")
