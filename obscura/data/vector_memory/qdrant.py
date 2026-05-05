"""Qdrant adapter — default vector backend.

Wraps :class:`obscura.vector_memory.backends.qdrant_backend.QdrantBackend`
with the new :class:`VectorMemoryRepo` Protocol shape via the shared
:class:`LegacyBackendAdapter`. Adds Qdrant-specific construction
(local file mode by default; cloud via ``QDRANT_URL``/``QDRANT_API_KEY``)
and turns init failures into structured :class:`VectorBackendUnavailable`.
"""

from __future__ import annotations

import logging

from obscura.data.vector_memory._legacy_adapter import LegacyBackendAdapter
from obscura.data.vector_memory.errors import VectorBackendUnavailable
from obscura.vector_memory.backends.base import BackendConfig
from obscura.vector_memory.backends.qdrant_backend import QdrantBackend

logger = logging.getLogger(__name__)


class QdrantVectorRepo(LegacyBackendAdapter):
    """Default :class:`VectorMemoryRepo` implementation."""

    def __init__(
        self,
        *,
        user_id: str,
        embedding_dim: int,
        mode: str = "local",
        path: str | None = None,
        url: str | None = None,
        api_key: str | None = None,
    ) -> None:
        try:
            backend = QdrantBackend(
                config=BackendConfig(user_id=user_id, embedding_dim=embedding_dim),
                mode=mode,
                path=path,
                url=url,
                api_key=api_key,
            )
        except Exception as exc:  # qdrant-client raises a wide variety
            raise VectorBackendUnavailable("qdrant", exc) from exc
        super().__init__(backend=backend, name="qdrant")
