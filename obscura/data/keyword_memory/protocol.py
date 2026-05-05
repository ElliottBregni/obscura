"""Domain types and Protocol for the keyword-memory repository."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class Memory:
    """A single recalled or stored memory record.

    `score` is populated by ``recall`` (FTS5 bm25 / Postgres ts_rank_cd,
    normalised so higher = better). On non-search paths the field is
    left at 0.0.
    """

    id: int
    namespace: str
    content: str
    metadata: dict[str, Any]
    created_at: float
    updated_at: float
    score: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "namespace": self.namespace,
            "content": self.content,
            "metadata": self.metadata,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "score": self.score,
        }


@runtime_checkable
class KeywordMemoryRepo(Protocol):
    """Backend-agnostic interface for the lazy keyword-memory store.

    Implemented by :class:`obscura.data.keyword_memory.sqlite.SqliteKeywordMemoryRepo`
    (FTS5) and :class:`obscura.data.keyword_memory.postgres.PostgresKeywordMemoryRepo`
    (tsvector). Callers should only depend on this Protocol — never
    import a backend directly.
    """

    def remember(
        self,
        content: str,
        *,
        namespace: str = "default",
        metadata: dict[str, Any] | None = None,
    ) -> int:
        """Insert a new memory; return its id."""
        ...

    def recall(
        self,
        query: str,
        *,
        namespace: str | None = None,
        top_k: int = 5,
    ) -> list[Memory]:
        """Keyword search; results ranked best-first."""
        ...

    def forget(self, memory_id: int) -> bool:
        """Delete a single memory; True if a row was removed."""
        ...

    def list_namespaces(self) -> list[tuple[str, int]]:
        """``(namespace, count)`` pairs sorted by namespace."""
        ...

    def list_by_namespace_prefix(
        self,
        prefix: str,
        *,
        limit: int = 50,
    ) -> list[Memory]:
        """Return memories whose namespace starts with *prefix*, newest first."""
        ...

    def stats(self) -> dict[str, Any]:
        """Diagnostic info: backend name, total count, per-namespace counts."""
        ...

    def close(self) -> None:
        """Release any held resources. May be a no-op."""
        ...
