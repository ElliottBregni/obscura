"""Vector memory backend implementations.

Available backends:
- SQLiteBackend: Local SQLite-based vector storage (default)
- QdrantBackend: Qdrant-based vector storage (scalable, fast)
"""

from obscura.vector_memory.backends.base import BackendConfig, VectorBackend, VectorEntry
from obscura.vector_memory.backends.sqlite_backend import SQLiteBackend

try:
    from obscura.vector_memory.backends.qdrant_backend import QdrantBackend

    QDRANT_AVAILABLE = True
except ImportError:
    QDRANT_AVAILABLE = False
    QdrantBackend = None  # type: ignore

__all__ = [
    "QDRANT_AVAILABLE",
    "BackendConfig",
    "QdrantBackend",
    "SQLiteBackend",
    "VectorBackend",
    "VectorEntry",
]
