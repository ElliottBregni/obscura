"""Base protocol and configuration for vector memory backends."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from obscura.memory import MemoryKey
from obscura.vector_memory.vector_memory_filters import MetadataFilter


@dataclass
class BackendConfig:
    """Configuration common to all vector backends."""

    user_id: str
    embedding_dim: int
    namespace: str | None = None


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
    score: float = 0.0


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
