"""Vector memory backend implementations.

Available backends:
- SQLiteBackend: Local SQLite-based vector storage (default)
- QdrantBackend: Qdrant-based vector storage (scalable, fast)
"""

from obscura.vector_memory.backends.base import (
    BackendConfig,
    VectorBackend,
    VectorEntry,
)
from obscura.vector_memory.backends.sqlite_backend import SQLiteBackend
import logging

logger = logging.getLogger(__name__)


_QdrantBackend: type[VectorBackend] | None
try:
    from obscura.vector_memory.backends.qdrant_backend import (
        QdrantBackend as _QdrantBackendImpl,
    )

    _QdrantBackend = _QdrantBackendImpl
    _qdrant_available = True
except ImportError:
    logger.debug("suppressed exception in <module>", exc_info=True)
    _QdrantBackend = None
    _qdrant_available = False

QdrantBackend = _QdrantBackend
QDRANT_AVAILABLE = _qdrant_available

__all__ = [
    "QDRANT_AVAILABLE",
    "BackendConfig",
    "QdrantBackend",
    "SQLiteBackend",
    "VectorBackend",
    "VectorEntry",
]
