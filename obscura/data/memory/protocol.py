"""Protocol for the key-value memory repository.

Re-exports :class:`MemoryEntry` and :class:`MemoryKey` from
:mod:`obscura.memory.types` so callers depend on a single import path.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Protocol, runtime_checkable

from obscura.memory.types import (
    MemoryEntry as MemoryEntry,
)
from obscura.memory.types import (
    MemoryKey as MemoryKey,
)


@runtime_checkable
class MemoryStore(Protocol):
    """Backend-agnostic key-value memory store.

    Per-user, namespace-scoped, TTL-aware. Both SQLite and Postgres
    implementations satisfy this Protocol — selection happens via
    :func:`obscura.data.memory.factory.get_memory_store`.
    """

    def set(  # noqa: ANN401  # arbitrary JSON-serialisable value
        self,
        key: str | MemoryKey,
        value: Any,
        namespace: str = "default",
        ttl: timedelta | None = None,
        source: str = "manual",
    ) -> None:
        """Insert or replace a value at ``(namespace, key)``."""
        ...

    def get(  # noqa: ANN401  # returns the stored value, shape unknown
        self,
        key: str | MemoryKey,
        namespace: str = "default",
    ) -> Any:
        """Return the value at ``(namespace, key)`` or None if missing/expired."""
        ...

    def delete(
        self,
        key: str | MemoryKey,
        namespace: str = "default",
    ) -> bool:
        """Delete one entry; True if a row was removed."""
        ...

    def list_keys(self, namespace: str | None = None) -> list[MemoryKey]:
        """List all keys, optionally scoped to a namespace."""
        ...

    def search(self, query: str) -> list[tuple[MemoryKey, Any]]:
        """Substring search across stored values; results may be empty."""
        ...

    def clear_namespace(self, namespace: str) -> int:
        """Delete every entry in *namespace*; returns count removed."""
        ...

    def clear_expired(self) -> int:
        """Delete entries whose TTL has elapsed; returns count removed."""
        ...

    def reap_expired(self) -> int:
        """Alias for :meth:`clear_expired` used by the reaper task."""
        ...

    def get_stats(self) -> dict[str, Any]:
        """Diagnostic info: backend, total entries, per-namespace counts."""
        ...

    def close(self) -> None:
        """Release any held resources."""
        ...
