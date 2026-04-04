"""Base protocol and configuration for vector memory backends."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from datetime import datetime

    from obscura.memory import MemoryKey
    from obscura.vector_memory.vector_memory_filters import MetadataFilter


@dataclass
class BackendConfig:
    """Configuration common to all vector backends."""

    user_id: str
    embedding_dim: int
    namespace: str | None = None
    decay_half_life_seconds: float | None = None


@dataclass
class VectorEntry:
    """A vector entry with metadata."""

    key: MemoryKey
    text: str
    embedding: list[float]
    metadata: dict[str, Any]
    memory_type: str
    created_at: datetime
    updated_at: datetime | None = None
    accessed_at: datetime | None = None
    score: float = 0.0
    rerank_score: float = 0.0
    final_score: float = 0.0


@runtime_checkable
class VectorBackend(Protocol):
    """Protocol defining the vector backend interface."""

    def store_vector(
        self,
        key: MemoryKey,
        text: str,
        embedding: list[float],
        metadata: dict[str, Any],
        memory_type: str,
        expires_at: datetime | None,
    ) -> None:
        """Store a vector with metadata."""
        ...

    def get_vector(self, key: MemoryKey) -> VectorEntry | None:
        """Retrieve a vector by key."""
        ...

    def search_vectors(
        self,
        query_embedding: list[float],
        namespace: str | None,
        top_k: int,
        threshold: float | None,
        filters: list[MetadataFilter] | None,
    ) -> list[VectorEntry]:
        """Search for similar vectors."""
        ...

    def delete_vector(self, key: MemoryKey) -> bool:
        """Delete a vector by key."""
        ...

    def list_keys(self, namespace: str | None = None) -> list[MemoryKey]:
        """List all keys."""
        ...

    def clear_namespace(self, namespace: str) -> int:
        """Clear all vectors in a namespace."""
        ...

    def get_stats(self) -> dict[str, Any]:
        """Get backend statistics."""
        ...

    def close(self) -> None:
        """Close backend connections."""
        ...

    def touch_vector(self, key: MemoryKey) -> None:
        """Update ``accessed_at`` to now.  No-op if key doesn't exist."""
        ...

    def list_by_type(
        self,
        memory_type: str,
        older_than: datetime | None = None,
        limit: int = 100,
    ) -> list[VectorEntry]:
        """List entries of a given type, optionally filtered by age."""
        ...

    def purge_expired(self) -> int:
        """Delete entries whose ``expires_at`` is in the past.  Returns count."""
        ...
