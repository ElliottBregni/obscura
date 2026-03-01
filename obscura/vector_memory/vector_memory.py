"""sdk/vector_memory — Semantic memory with vector search.

Extends the memory system with embeddings and similarity search.
Agents can store memories and retrieve semantically similar ones.

Usage::

    from obscura.vector_memory import VectorMemoryStore

    store = VectorMemoryStore.for_user(user)

    # Store with automatic embedding
    store.set("python_async", "Async/await is Python's way to handle concurrency...")

    # Semantic search
    results = store.search_similar(
        "how do I run multiple things at once?",
        top_k=3
    )
    # Returns memories about async/concurrency even if keywords don't match
"""

from __future__ import annotations

import hashlib
import os
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np

from obscura.auth.models import AuthenticatedUser
from obscura.memory import MemoryKey
from obscura.vector_memory.backends import (
    BackendConfig,
    SQLiteBackend,
    VectorBackend,
    VectorEntry,
)

try:
    from obscura.vector_memory.backends import QDRANT_AVAILABLE, QdrantBackend
except ImportError:
    QDRANT_AVAILABLE = False
    QdrantBackend = None  # type: ignore
from obscura.vector_memory.vector_memory_filters import MetadataFilter


# Simple embedding function (in production, use OpenAI, sentence-transformers, etc.)
def simple_embedding(text: str, dim: int = 384) -> list[float]:
    """Create a simple hash-based embedding for demo purposes.

    In production, replace with:
    - OpenAI text-embedding-3-small
    - sentence-transformers/all-MiniLM-L6-v2
    - Custom embedding model
    """
    # Hash the text to get deterministic "embedding"
    hash_bytes = hashlib.sha256(text.encode()).digest()

    # Convert to float array
    floats: list[float] = []
    for i in range(0, len(hash_bytes), 4):
        chunk = hash_bytes[i : i + 4]
        val = int.from_bytes(chunk, "little", signed=True)
        floats.append(val / 2**31)  # Normalize to [-1, 1]

    # Pad or truncate to desired dimension
    if len(floats) < dim:
        floats = floats * (dim // len(floats) + 1)

    return floats[:dim]


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    a_arr = np.array(a)
    b_arr = np.array(b)

    norm_a = np.linalg.norm(a_arr)
    norm_b = np.linalg.norm(b_arr)

    if norm_a == 0 or norm_b == 0:
        return 0.0

    return float(np.dot(a_arr, b_arr) / (norm_a * norm_b))


# Alias VectorEntry from backend for backwards compatibility
VectorMemoryEntry = VectorEntry


class VectorMemoryStore:
    """Semantic memory store with pluggable vector backends.

    Supports multiple backends:
    - SQLiteBackend: Local file-based storage (default)
    - QdrantBackend: High-performance vector search (100-1000x faster)

    Each user gets an isolated backend instance with:
    - Text content
    - Vector embeddings
    - Metadata
    - Efficient similarity search
    """

    _instances: dict[str, VectorMemoryStore] = {}
    _lock = threading.Lock()  # Type annotation placeholder

    def __init__(
        self,
        user: AuthenticatedUser,
        backend: VectorBackend | None = None,
        embedding_fn: Callable[[str], list[float]] | None = None,
    ):
        self.user = user
        self.user_id = user.user_id

        self.embedding_fn = embedding_fn or simple_embedding
        self.embedding_dim = len(self.embedding_fn("test"))

        # Initialize backend
        if backend is None:
            backend = self._create_default_backend()

        self.backend = backend

    def _create_default_backend(self) -> VectorBackend:
        """Create the default backend based on environment configuration."""
        import hashlib
        backend_type = os.environ.get("OBSCURA_VECTOR_BACKEND", "qdrant").lower()

        config = BackendConfig(
            user_id=self.user_id,
            embedding_dim=self.embedding_dim,
            namespace=None,
        )

        # Prefer Qdrant if available, fallback to SQLite
        if backend_type == "qdrant":
            if QDRANT_AVAILABLE:
                mode = os.environ.get("OBSCURA_QDRANT_MODE", "local")
                path = os.environ.get("OBSCURA_QDRANT_PATH")
                url = os.environ.get("OBSCURA_QDRANT_URL")
                api_key = os.environ.get("OBSCURA_QDRANT_API_KEY")

                return QdrantBackend(
                    config=config,
                    mode=mode,
                    path=path,
                    url=url,
                    api_key=api_key,
                )
            # Qdrant not available, fall back to SQLite
            backend_type = "sqlite"

        # SQLite backend (default fallback if Qdrant unavailable)
        base_dir = Path(
            os.environ.get(
                "OBSCURA_VECTOR_MEMORY_DIR",
                Path.home() / ".obscura" / "vector_memory",
            ),
        )
        db_id = hashlib.sha256(self.user_id.encode()).hexdigest()[:16]
        db_path = base_dir / f"{db_id}.db"

        return SQLiteBackend(config=config, db_path=db_path)

    @classmethod
    def for_user(
        cls,
        user: AuthenticatedUser,
        embedding_fn: Callable[[str], list[float]] | None = None,
    ) -> VectorMemoryStore:
        """Get or create a vector memory store for the given user."""
        with cls._lock:
            if user.user_id not in cls._instances:
                cls._instances[user.user_id] = cls(user, embedding_fn=embedding_fn)
            return cls._instances[user.user_id]

    @classmethod
    def reset_instances(cls) -> None:
        """Clear singleton cache. For testing only."""
        with cls._lock:
            cls._instances.clear()


    def set(
        self,
        key: str | MemoryKey,
        text: str,
        metadata: dict[str, Any] | None = None,
        namespace: str = "default",
        ttl: timedelta | None = None,
        memory_type: str = "general",
    ) -> None:
        """Store text with automatic embedding generation.

        Args:
            key: The memory key
            text: The text content to store and embed
            metadata: Additional JSON-serializable metadata
            namespace: Logical grouping
            ttl: Optional time-to-live
            memory_type: Classification (fact, preference, episode, summary, etc.)

        """
        if isinstance(key, str):
            key = MemoryKey(namespace=namespace, key=key)

        # Generate embedding
        embedding = self.embedding_fn(text)

        expires_at = None
        if ttl:
            expires_at = datetime.now(UTC) + ttl

        self.backend.store_vector(
            key=key,
            text=text,
            embedding=embedding,
            metadata=metadata or {},
            memory_type=memory_type,
            expires_at=expires_at,
        )

    def get(
        self, key: str | MemoryKey, namespace: str = "default",
    ) -> VectorMemoryEntry | None:
        """Retrieve a specific memory entry by key."""
        if isinstance(key, str):
            key = MemoryKey(namespace=namespace, key=key)

        entry = self.backend.get_vector(key)

        if entry is None:
            return None

        # Backend may not handle expiration automatically
        # Check if entry has expired (for backwards compatibility)
        return entry

    def search_similar(
        self,
        query: str,
        namespace: str | None = None,
        top_k: int = 5,
        threshold: float = -1.0,
        memory_types: list[str] | None = None,
        metadata_filters: list[MetadataFilter] | None = None,
        date_range: tuple[datetime, datetime] | None = None,
    ) -> list[VectorMemoryEntry]:
        """Search for semantically similar memories.

        Args:
            query: The search query text
            namespace: Filter by namespace (None = all)
            top_k: Number of results to return
            threshold: Minimum similarity score (0-1)
            memory_types: Filter to specific memory types
            metadata_filters: List of MetadataFilter objects for SQL pre-filtering
            date_range: (start, end) tuple to filter by created_at

        Returns:
            List of memories sorted by similarity (highest first)

        """
        from obscura.vector_memory.vector_memory_filters import (
            DateRangeFilter,
            MemoryTypeFilter,
        )

        query_embedding = self.embedding_fn(query)

        # Build filter list
        filters: list[MetadataFilter] = list(metadata_filters or [])
        if memory_types:
            filters.append(MemoryTypeFilter(memory_types=memory_types))
        if date_range:
            filters.append(
                DateRangeFilter(
                    field="created_at", start=date_range[0], end=date_range[1],
                ),
            )

        results = self.backend.search_vectors(
            query_embedding=query_embedding,
            namespace=namespace,
            top_k=top_k,
            threshold=threshold,
            filters=filters or None,
        )

        return results

    def search_reranked(
        self,
        query: str,
        namespace: str | None = None,
        top_k: int = 5,
        first_stage_k: int = 50,
        threshold: float = -1.0,
        memory_types: list[str] | None = None,
        metadata_filters: list[MetadataFilter] | None = None,
        date_range: tuple[datetime, datetime] | None = None,
        reranker: Any | None = None,
        recency_weight: float = 0.2,
    ) -> list[VectorMemoryEntry]:
        """Two-stage retrieval with reranking.

        Stage 1: Vector similarity search to get a candidate pool.
        Stage 2: Rerank candidates with additional signals (recency, BM25, metadata).

        Args:
            query: The search query text
            namespace: Filter by namespace
            top_k: Final number of results after reranking
            first_stage_k: Candidate pool size from stage 1
            threshold: Minimum similarity score for stage 1
            memory_types: Filter to specific memory types
            metadata_filters: SQL pre-filters
            date_range: (start, end) created_at filter
            reranker: A Reranker instance (default: RecencyReranker)
            recency_weight: Weight for default RecencyReranker

        Returns:
            List of memories sorted by final_score (highest first)

        """
        from obscura.vector_memory.vector_memory_rerank import RecencyReranker

        # Stage 1: get candidate pool
        candidates = self.search_similar(
            query=query,
            namespace=namespace,
            top_k=first_stage_k,
            threshold=threshold,
            memory_types=memory_types,
            metadata_filters=metadata_filters,
            date_range=date_range,
        )

        if not candidates:
            return []

        # Stage 2: rerank
        if reranker is None:
            reranker = RecencyReranker(weight=recency_weight)

        query_embedding = self.embedding_fn(query)

        for entry in candidates:
            entry.rerank_score = reranker.score(query, entry, query_embedding)
            entry.final_score = entry.score + entry.rerank_score

        candidates.sort(key=lambda x: x.final_score, reverse=True)
        return candidates[:top_k]

    def delete(self, key: str | MemoryKey, namespace: str = "default") -> bool:
        """Delete a memory entry."""
        if isinstance(key, str):
            key = MemoryKey(namespace=namespace, key=key)

        return self.backend.delete_vector(key)

    def list_keys(self, namespace: str | None = None) -> list[MemoryKey]:
        """List all memory keys."""
        return self.backend.list_keys(namespace=namespace)

    def clear_namespace(self, namespace: str) -> int:
        """Clear all memories in a namespace."""
        return self.backend.clear_namespace(namespace)

    def get_stats(self) -> dict[str, Any]:
        """Get vector memory statistics."""
        backend_stats = self.backend.get_stats()

        return {
            "total_memories": backend_stats.get("total_count", 0),
            "embedding_dim": self.embedding_dim,
            "namespaces": backend_stats.get("namespaces", {}),
            "backend": backend_stats.get("backend_type", "unknown"),
        }

    def close(self) -> None:
        """Close the database connection."""
        self.backend.close()


# Integration with Agent class
class SemanticMemoryMixin:
    """Mixin to add semantic memory capabilities to agents."""

    # Attributes expected from the host class (e.g., Agent)
    user: AuthenticatedUser
    id: str
    config: Any  # AgentConfig — avoids circular import

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._vector_memory: VectorMemoryStore | None = None

    @property
    def vector_memory(self) -> VectorMemoryStore:
        """Get the vector memory store for this agent."""
        if self._vector_memory is None:
            self._vector_memory = VectorMemoryStore.for_user(self.user)
        return self._vector_memory

    def remember(
        self,
        text: str,
        key: str | None = None,
        memory_type: str = "general",
        **metadata: Any,
    ) -> None:
        """Store a memory with semantic embedding."""
        if key is None:
            key = f"memory_{datetime.now(UTC).timestamp()}"

        self.vector_memory.set(
            key,
            text,
            metadata={"agent_id": self.id, "agent_name": self.config.name, **metadata},
            namespace=f"{self.config.memory_namespace}:semantic",
            memory_type=memory_type,
        )

    def recall(
        self,
        query: str,
        top_k: int = 3,
        memory_types: list[str] | None = None,
        use_reranking: bool = True,
        recency_weight: float = 0.2,
    ) -> list[VectorMemoryEntry]:
        """Recall semantically similar memories with optional reranking."""
        namespace = f"{self.config.memory_namespace}:semantic"

        if use_reranking:
            return self.vector_memory.search_reranked(
                query,
                namespace=namespace,
                top_k=top_k,
                memory_types=memory_types,
                recency_weight=recency_weight,
            )

        return self.vector_memory.search_similar(
            query,
            namespace=namespace,
            top_k=top_k,
            memory_types=memory_types,
        )
