"""sdk/memory — Shared memory database for AI agents.

Multi-tenant memory storage scoped by auth token.
Agents can read/write key-value pairs, search semantically, and maintain
conversation history.

Usage::

    from obscura.memory import MemoryStore

    store = MemoryStore.for_user(user)
    store.set("project_context", {"repo": "obscura", "tech": "python"})
    context = store.get("project_context")

Layout
------
This package's ``__init__`` is a thin re-export surface so siblings
(``events``, ``postgres_memory``, ``store``) can depend on the leaf
``types`` module without going through the package, which avoids
partial-init cycles. Concrete implementations live in:

- ``obscura.memory.types``           — ``MemoryKey``, ``MemoryEntry``
- ``obscura.memory.events``          — event sinks
- ``obscura.memory.store``           — ``MemoryStore``, ``GlobalMemoryStore``
- ``obscura.memory.postgres_memory`` — PG implementation
"""

from __future__ import annotations

from obscura.memory.store import (
    GlobalMemoryStore,
    MemoryStore,
    create_memory_store,
)
from obscura.memory.types import MemoryEntry, MemoryKey

__all__ = [
    "GlobalMemoryStore",
    "MemoryEntry",
    "MemoryKey",
    "MemoryStore",
    "create_memory_store",
]
