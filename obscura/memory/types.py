"""obscura.memory.types — Pure data types for the memory subsystem.

Lives at the bottom of the memory package so that sibling modules
(``events``, ``postgres_memory``, ``store``) can depend on these types
without going through ``obscura/memory/__init__.py``. That used to force
all consumers of ``MemoryKey`` to lazy-import to break the cycle.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, override, runtime_checkable


@dataclass(frozen=True)
class MemoryKey:
    """A namespaced memory key."""

    namespace: str  # e.g., "session", "project", "user", "global"
    key: str  # e.g., "context", "preferences", "history"

    @override
    def __str__(self) -> str:
        return f"{self.namespace}:{self.key}"


@dataclass
class MemoryEntry:
    """A single memory entry with metadata."""

    key: MemoryKey
    value: Any
    created_at: datetime
    updated_at: datetime
    ttl: timedelta | None = None  # Time-to-live for ephemeral memory

    @property
    def is_expired(self) -> bool:
        if self.ttl is None:
            return False
        return datetime.now(UTC) > self.updated_at + self.ttl


@runtime_checkable
class MemoryStoreProtocol(Protocol):
    """Public surface shared by SQLite and Postgres memory stores.

    Both impls expose this contract; the factory
    :func:`obscura.memory.store.create_memory_store` returns a value
    typed against this Protocol so callers don't bind to a specific
    backend.
    """

    user_id: str

    def set(
        self,
        key: str | MemoryKey,
        value: Any,
        namespace: str = "default",
        ttl: timedelta | None = None,
    ) -> None: ...

    def get(
        self,
        key: str | MemoryKey,
        namespace: str = "default",
        default: Any = None,
    ) -> Any: ...

    def delete(self, key: str | MemoryKey, namespace: str = "default") -> bool: ...

    def list_keys(self, namespace: str | None = None) -> list[MemoryKey]: ...

    def search(self, query: str) -> list[tuple[MemoryKey, Any]]: ...

    def clear_namespace(self, namespace: str) -> int: ...

    def clear_expired(self) -> int: ...

    def get_stats(self) -> dict[str, Any]: ...

    def close(self) -> None: ...
