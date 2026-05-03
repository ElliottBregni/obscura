"""obscura.memory.types — Pure data types for the memory subsystem.

Lives at the bottom of the memory package so that sibling modules
(``events``, ``postgres_memory``, ``store``) can depend on these types
without going through ``obscura/memory/__init__.py``. That used to force
all consumers of ``MemoryKey`` to lazy-import to break the cycle.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, override


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
