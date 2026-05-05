"""Domain types and Protocol for the vector-memory repository.

Higher-level than the legacy :class:`obscura.vector_memory.backends.base.VectorBackend`
— exposes the verbs the application actually wants
(``upsert`` / ``search`` / ``payload_filter`` / ``count`` / ``delete``)
and pushes embedding-management details (touch, gc, list_by_type) into
backend-specific operational tools rather than every caller's
vocabulary.

Implementations live in ``qdrant.py`` (default), ``pgvector.py``, and
``sqlite_vss.py``. Callers obtain a Protocol-typed instance from
:func:`obscura.data.vector_memory.factory.get_vector_memory_repo` and
never import a backend directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class VectorRecord:
    """A single vector entry — input for upsert, output of search."""

    namespace: str
    key: str
    text: str
    embedding: list[float] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    score: float = 0.0  # populated by search; 0.0 on store paths

    def to_dict(self) -> dict[str, Any]:
        return {
            "namespace": self.namespace,
            "key": self.key,
            "text": self.text,
            "metadata": self.metadata,
            "score": self.score,
        }


@runtime_checkable
class VectorMemoryRepo(Protocol):
    """Backend-agnostic vector repository.

    Every method may raise :class:`obscura.data.vector_memory.errors.VectorMemoryError`
    or a subclass; callers should catch that one base class. Transient
    failures inside the backend are retried internally where appropriate
    — :class:`VectorRetryExhausted` is raised only after the budget is
    spent.
    """

    backend_name: str

    def upsert(self, records: list[VectorRecord]) -> int:
        """Insert or update records. Returns count written."""
        ...

    def search(
        self,
        query_embedding: list[float],
        *,
        namespace: str | None = None,
        top_k: int = 5,
        score_threshold: float | None = None,
    ) -> list[VectorRecord]:
        """Cosine-similarity search; ranked best-first."""
        ...

    def payload_filter(
        self,
        *,
        namespace: str | None = None,
        metadata: dict[str, Any] | None = None,
        top_k: int = 50,
    ) -> list[VectorRecord]:
        """Find records by exact-match metadata, no similarity scoring."""
        ...

    def delete(self, namespace: str, key: str) -> bool:
        """Delete one record by ``(namespace, key)``. True if removed."""
        ...

    def count(self, *, namespace: str | None = None) -> int:
        """Total record count, optionally scoped to a namespace."""
        ...

    def healthcheck(self) -> bool:
        """Cheap probe — True iff the backend is reachable. Never raises."""
        ...

    def close(self) -> None:
        """Release any held resources."""
        ...
