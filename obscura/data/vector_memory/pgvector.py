"""pgvector adapter — opt-in fallback for the vector-memory repository.

Selected only when ``OBSCURA_VECTOR_BACKEND=pgvector``. Wraps
:class:`obscura.vector_memory.backends.postgres_backend.PostgreSQLVectorBackend`
through the shared :class:`LegacyBackendAdapter`.
"""

from __future__ import annotations

import logging

from obscura.data.vector_memory._legacy_adapter import LegacyBackendAdapter
from obscura.data.vector_memory.errors import VectorBackendUnavailable
from obscura.vector_memory.backends.base import BackendConfig
from obscura.vector_memory.backends.postgres_backend import PostgreSQLVectorBackend

logger = logging.getLogger(__name__)


class PgvectorVectorRepo(LegacyBackendAdapter):
    """Postgres pgvector :class:`VectorMemoryRepo` implementation."""

    def __init__(self, *, user_id: str, embedding_dim: int) -> None:
        try:
            backend = PostgreSQLVectorBackend(
                config=BackendConfig(user_id=user_id, embedding_dim=embedding_dim),
            )
        except Exception as exc:
            raise VectorBackendUnavailable("pgvector", exc) from exc
        super().__init__(backend=backend, name="pgvector")
