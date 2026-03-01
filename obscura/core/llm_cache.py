"""obscura.core.llm_cache — In-memory LLM response cache with TTL.

Opt-in cache for ``ObscuraClient.send()`` responses. Caches by a
deterministic hash of (backend, model, system_prompt, prompt).

NOT cached: ``stream()``, ``run_loop()``, or responses with tool calls.
"""

from __future__ import annotations

import hashlib
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass


@dataclass(frozen=True)
class CacheStats:
    """Cache hit/miss/eviction statistics."""

    hits: int
    misses: int
    evictions: int
    entries: int
    max_entries: int


@dataclass
class CacheEntry:
    """A single cached LLM response."""

    key: str
    response_text: str
    backend: str
    model: str
    created_at: float
    ttl_seconds: float
    hit_count: int = 0

    def is_expired(self, now: float | None = None) -> bool:
        """Check if this entry has expired."""
        if now is None:
            now = time.monotonic()
        return (now - self.created_at) >= self.ttl_seconds


class LLMCache:
    """In-memory LLM response cache with TTL and LRU eviction.

    Thread-safe. Designed for single-process deployment.

    Parameters
    ----------
    max_entries:
        Maximum number of cached responses.
    default_ttl:
        Default time-to-live in seconds for cache entries.
    """

    def __init__(
        self,
        *,
        max_entries: int = 1000,
        default_ttl: float = 300.0,
    ) -> None:
        self._max_entries = max_entries
        self._default_ttl = default_ttl
        self._entries: OrderedDict[str, CacheEntry] = OrderedDict()
        self._lock = threading.Lock()

        # Stats
        self._hits = 0
        self._misses = 0
        self._evictions = 0

    def get(self, key: str) -> CacheEntry | None:
        """Lookup by key. Returns None if miss or expired."""
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                self._misses += 1
                return None

            if entry.is_expired():
                del self._entries[key]
                self._misses += 1
                return None

            # Move to end (most recently used)
            self._entries.move_to_end(key)
            entry.hit_count += 1
            self._hits += 1
            return entry

    def put(
        self,
        key: str,
        response_text: str,
        *,
        backend: str,
        model: str,
        ttl: float | None = None,
    ) -> None:
        """Store a response. Evicts oldest if at capacity (LRU)."""
        with self._lock:
            if key in self._entries:
                # Update existing
                del self._entries[key]

            # Evict if at capacity
            while len(self._entries) >= self._max_entries:
                self._entries.popitem(last=False)
                self._evictions += 1

            self._entries[key] = CacheEntry(
                key=key,
                response_text=response_text,
                backend=backend,
                model=model,
                created_at=time.monotonic(),
                ttl_seconds=ttl if ttl is not None else self._default_ttl,
            )

    def invalidate(self, key: str) -> bool:
        """Remove a specific entry. Returns True if it existed."""
        with self._lock:
            if key in self._entries:
                del self._entries[key]
                return True
            return False

    def clear(self) -> None:
        """Purge all entries."""
        with self._lock:
            self._entries.clear()

    def stats(self) -> CacheStats:
        """Return hit/miss/eviction counts."""
        with self._lock:
            return CacheStats(
                hits=self._hits,
                misses=self._misses,
                evictions=self._evictions,
                entries=len(self._entries),
                max_entries=self._max_entries,
            )

    @staticmethod
    def make_key(
        backend: str, model: str, system_prompt: str, prompt: str
    ) -> str:
        """Deterministic cache key from request parameters.

        Uses SHA-256 for collision resistance.
        """
        raw = f"{backend}:{model}:{system_prompt}:{prompt}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()
