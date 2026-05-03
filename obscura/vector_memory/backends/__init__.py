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

try:
    from obscura.vector_memory.backends.qdrant_backend import QdrantBackend

    _qdrant_available = True
except ImportError:
    _qdrant_available = False
    QdrantBackend = None

QDRANT_AVAILABLE: bool = _qdrant_available

__all__ = [
    "QDRANT_AVAILABLE",
    "BackendConfig",
    "QdrantBackend",
    "SQLiteBackend",
    "VectorBackend",
    "VectorEntry",
]
