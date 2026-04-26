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
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

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
    QDRANT_AVAILABLE: bool

    def QdrantBackend(*args: Any, **kwargs: Any) -> None:  # type: ignore[misc]
        """Stub when qdrant-client is not installed."""


import contextlib

from obscura.vector_memory.decay import DecayConfig, load_decay_config_from_disk

if TYPE_CHECKING:
    from collections.abc import Callable

    from obscura.auth.models import AuthenticatedUser
    from obscura.memory.events import EventSink
    from obscura.vector_memory.vector_memory_filters import MetadataFilter

_log = logging.getLogger(__name__)


def simple_embedding(text: str, dim: int = 384) -> list[float]:
    """Deterministic hash-based embedding — used as fallback only.

    Not semantically meaningful. For real semantic search, set an
    embedding_fn when constructing VectorMemoryStore, or ensure
    sentence-transformers is installed (auto-detected below).
    """
    hash_bytes = hashlib.sha256(text.encode()).digest()
    floats: list[float] = []
    for i in range(0, len(hash_bytes), 4):
        chunk = hash_bytes[i : i + 4]
        val = int.from_bytes(chunk, "little", signed=True)
        floats.append(val / 2**31)
    if len(floats) < dim:
        floats = floats * (dim // len(floats) + 1)
    return floats[:dim]


def _make_default_embedding_fn(dim: int = 384):
    """Return the best available embedding function.

    Priority:
    1. sentence-transformers all-MiniLM-L6-v2 (local, no API key, real semantics)
    2. simple_embedding (hash-based fallback, deterministic but not semantic)
    """
    try:
        import logging as _logging
        import os as _os

        from sentence_transformers import SentenceTransformer  # type: ignore[import]

        _log = _logging.getLogger(__name__)
        # Suppress noisy model loading output (position_ids warning, progress bars, HF auth)
        _env_overrides = {
            "TRANSFORMERS_VERBOSITY": "error",
            "HF_HUB_DISABLE_PROGRESS_BARS": "1",
            "HF_HUB_VERBOSITY": "error",
        }
        _prev_env = {k: _os.environ.get(k) for k in _env_overrides}
        _os.environ.update(_env_overrides)
        for _logger_name in (
            "transformers",
            "sentence_transformers",
            "huggingface_hub",
        ):
            _logging.getLogger(_logger_name).setLevel(_logging.ERROR)
        _model = SentenceTransformer("all-MiniLM-L6-v2")
        for _logger_name in (
            "transformers",
            "sentence_transformers",
            "huggingface_hub",
        ):
            _logging.getLogger(_logger_name).setLevel(_logging.WARNING)
        for k, v in _prev_env.items():
            if v is None:
                _os.environ.pop(k, None)
            else:
                _os.environ[k] = v
        _log.info(
            "vector_memory: using sentence-transformers/all-MiniLM-L6-v2 for embeddings",
        )

        def _st_embed(text: str) -> list[float]:
            return _model.encode(text, normalize_embeddings=True).tolist()

        return _st_embed
    except ImportError:
        import logging as _logging

        _logging.getLogger(__name__).warning(
            "vector_memory: sentence-transformers not installed, "
            "falling back to hash-based embedding (not semantic). "
            "Install with: pip install sentence-transformers",
        )
        return simple_embedding


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
        decay_config: DecayConfig | None = None,
        event_sink: EventSink | None = None,
    ) -> None:
        self.user = user
        self.user_id = user.user_id
        self.decay_config = decay_config or load_decay_config_from_disk()

        self.embedding_fn = embedding_fn or _make_default_embedding_fn()
        self.embedding_dim = len(self.embedding_fn("test"))

        # Initialize backend
        if backend is None:
            backend = self._create_default_backend()

        self.backend = backend
        self._event_sink = event_sink

    def _emit(
        self,
        kind: str,
        key: MemoryKey,
        value: Any | None,
        ttl_seconds: float | None,
    ) -> None:
        """Emit a memory event after the backend has accepted the write."""
        from obscura.memory.events import EventKind, get_default_sink, make_event

        sink = self._event_sink if self._event_sink is not None else get_default_sink()
        event_kind: EventKind = kind  # type: ignore[assignment]
        sink.emit(
            make_event(
                kind=event_kind,
                key=key,
                value=value,
                ttl_seconds=ttl_seconds,
                source="vector",
                user_id=self.user_id,
            ),
        )

    def _create_default_backend(self) -> VectorBackend:
        """Create the default backend based on environment configuration."""
        import hashlib
        import logging

        logger = logging.getLogger(__name__)
        backend_type = os.environ.get("OBSCURA_VECTOR_BACKEND", "").lower()
        if not backend_type:
            # Auto-detect from OBSCURA_DB_TYPE
            db_type = os.environ.get("OBSCURA_DB_TYPE", "sqlite").lower()
            backend_type = "postgresql" if db_type == "postgresql" else "qdrant"

        try:
            half_life = float(os.environ.get("OBSCURA_MEMORY_DECAY_HALF_LIFE_SECONDS"))
        except Exception:
            half_life = None
        config = BackendConfig(
            user_id=self.user_id,
            embedding_dim=self.embedding_dim,
            namespace=None,
            decay_half_life_seconds=half_life,
        )

        # Prefer Qdrant if available, fallback to SQLite
        if backend_type == "qdrant":
            if QDRANT_AVAILABLE:
                mode = os.environ.get("OBSCURA_QDRANT_MODE", "local")
                path = os.environ.get("OBSCURA_QDRANT_PATH")
                url = os.environ.get("OBSCURA_QDRANT_URL")
                api_key = os.environ.get("OBSCURA_QDRANT_API_KEY")
                try:
                    return QdrantBackend(
                        config=config,
                        decay_config=self.decay_config,
                        mode=mode,
                        path=path,
                        url=url,
                        api_key=api_key,
                    )
                except Exception as e:
                    # If Qdrant client exists but remote is unreachable, fall back to sqlite
                    logger.warning(
                        "Qdrant backend initialization failed, falling back to SQLite: %s",
                        e,
                    )
                    backend_type = "sqlite"
            else:
                # Qdrant not available, fall back to SQLite
                backend_type = "sqlite"

        # PostgreSQL backend
        if backend_type == "postgresql":
            try:
                from obscura.vector_memory.backends.postgres_backend import (
                    PostgreSQLVectorBackend,
                )

                return PostgreSQLVectorBackend(config=config)
            except Exception as e:
                logger.warning(
                    "PostgreSQL vector backend failed, falling back to SQLite: %s",
                    e,
                )
                backend_type = "sqlite"

        # SQLite backend (default fallback)
        base_dir = Path(
            os.environ.get(
                "OBSCURA_VECTOR_MEMORY_DIR",
                Path.home() / ".obscura" / "vector_memory",
            ),
        )
        db_id = hashlib.sha256(self.user_id.encode()).hexdigest()[:16]
        db_path = base_dir / f"{db_id}.db"

        return SQLiteBackend(
            config=config,
            db_path=db_path,
            decay_config=self.decay_config,
        )

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
        self._emit(
            "set",
            key,
            text,
            ttl.total_seconds() if ttl else None,
        )

    def get(
        self,
        key: str | MemoryKey,
        namespace: str = "default",
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
                    field="created_at",
                    start=date_range[0],
                    end=date_range[1],
                ),
            )

        results = self.backend.search_vectors(
            query_embedding=query_embedding,
            namespace=namespace,
            top_k=top_k,
            threshold=threshold,
            filters=filters or None,
        )

        # Ensure results are sorted by final_score (backend may apply decay), fallback to score.
        for r in results:
            if not getattr(r, "final_score", None):
                r.final_score = r.score or 0.0
        results.sort(
            key=lambda x: getattr(x, "final_score", x.score or 0.0),
            reverse=True,
        )
        return results[:top_k]

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
        recency_weight: float = 0.4,
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
            reranker = RecencyReranker(
                weight=recency_weight,
                decay_config=self.decay_config,
            )

        query_embedding = self.embedding_fn(query)

        for entry in candidates:
            entry.rerank_score = reranker.score(query, entry, query_embedding)
            entry.final_score = entry.score * entry.rerank_score

        candidates.sort(key=lambda x: x.final_score, reverse=True)
        return candidates[:top_k]

    def delete(self, key: str | MemoryKey, namespace: str = "default") -> bool:
        """Delete a memory entry."""
        if isinstance(key, str):
            key = MemoryKey(namespace=namespace, key=key)

        existed = self.backend.delete_vector(key)
        if existed:
            self._emit("delete", key, None, None)
        return existed

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
            "total_memories": backend_stats.get(
                "total_vectors",
                backend_stats.get("total_count", 0),
            ),
            "embedding_dim": backend_stats.get("embedding_dim", self.embedding_dim),
            "namespaces": backend_stats.get("namespaces", {}),
            "backend": backend_stats.get(
                "backend",
                backend_stats.get("backend_type", "unknown"),
            ),
        }

    def touch(self, key: str | MemoryKey, namespace: str = "default") -> None:
        """Update ``accessed_at`` to now, refreshing effective age for decay."""
        if isinstance(key, str):
            key = MemoryKey(namespace=namespace, key=key)
        self.backend.touch_vector(key)

    def _touch_results_async(self, entries: list[VectorEntry]) -> None:
        """Touch all entries in a background thread (fire-and-forget)."""

        def _do() -> None:
            for e in entries:
                with contextlib.suppress(Exception):
                    self.backend.touch_vector(e.key)

        # daemon=True: fire-and-forget touch — data loss on exit is acceptable
        # because touch only updates access timestamps (best-effort freshness).
        t = threading.Thread(target=_do, daemon=True)
        t.start()

    def run_maintenance(self) -> MaintenanceReport:
        """Purge expired entries and consolidate old episodes.

        Returns a :class:`MaintenanceReport` with counts.
        """
        start = time.monotonic()
        expired = 0
        consolidated = 0
        summaries = 0

        # 1. Purge expired
        try:
            expired = self.backend.purge_expired()
        except Exception:
            _log.debug("purge_expired failed", exc_info=True)

        # 2. Consolidation (import here to avoid circular)
        try:
            from obscura.vector_memory.consolidator import MemoryConsolidator

            consolidator = MemoryConsolidator(store=self, config=self.decay_config)
            consolidated, summaries = consolidator.consolidate()
        except Exception:
            _log.debug("consolidation failed", exc_info=True)

        duration = (time.monotonic() - start) * 1000
        report = MaintenanceReport(
            expired_purged=expired,
            episodes_consolidated=consolidated,
            summaries_created=summaries,
            duration_ms=duration,
        )
        _log.info(
            "memory maintenance: purged=%d consolidated=%d summaries=%d (%.0fms)",
            expired,
            consolidated,
            summaries,
            duration,
        )
        return report

    def close(self) -> None:
        """Close the database connection."""
        self.backend.close()


@dataclass
class MaintenanceReport:
    """Result of a :meth:`VectorMemoryStore.run_maintenance` run."""

    expired_purged: int = 0
    episodes_consolidated: int = 0
    summaries_created: int = 0
    duration_ms: float = 0.0


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
        *,
        use_graph: bool = True,
        use_reranking: bool = True,
        recency_weight: float = 0.2,
    ) -> list[VectorMemoryEntry]:
        """Recall semantically similar memories.

        Routes through the hybrid (graph-aware) path when the underlying
        store is a :class:`HybridVectorMemoryStore` and ``use_graph`` is
        True (default). Otherwise falls back to two-stage rerank, then to
        plain similarity.
        """
        namespace = f"{self.config.memory_namespace}:semantic"

        if use_graph:
            try:
                from obscura.lightrag_memory.hybrid_store import (
                    HybridVectorMemoryStore,
                )

                if isinstance(self.vector_memory, HybridVectorMemoryStore):
                    return self.vector_memory.search_hybrid(
                        query,
                        namespace=namespace,
                        top_k=top_k,
                        memory_types=memory_types,
                    )
            except ImportError:
                pass

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
