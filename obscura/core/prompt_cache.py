"""
obscura.core.prompt_cache — Prompt caching strategy.

Hashes the system prompt + tool schemas to detect cache hits/misses
and avoid re-sending identical prompt prefixes to the API.

When the hash matches the previous request, the API can serve from
its prompt cache, saving tokens and cost.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class CacheStats:
    """Prompt cache hit/miss statistics."""

    hits: int = 0
    misses: int = 0
    last_hash: str = ""
    last_hit_at: float = 0.0


class PromptCacheManager:
    """Track prompt cache state across turns.

    Usage::

        cache = PromptCacheManager()
        is_hit = cache.check(system_prompt, tool_schemas)
        if is_hit:
            # API will likely serve from cache
            pass
    """

    def __init__(self) -> None:
        self._stats = CacheStats()
        self._previous_hash = ""

    def compute_hash(
        self,
        system_prompt: str,
        tool_schemas: list[dict[str, Any]] | None = None,
    ) -> str:
        """Compute a deterministic hash of the prompt prefix."""
        hasher = hashlib.sha256()
        hasher.update(system_prompt.encode("utf-8"))
        if tool_schemas:
            # Sort tool schemas by name for deterministic hashing.
            sorted_schemas = sorted(tool_schemas, key=lambda s: s.get("name", ""))
            for schema in sorted_schemas:
                hasher.update(str(schema).encode("utf-8"))
        return hasher.hexdigest()[:16]

    def check(
        self,
        system_prompt: str,
        tool_schemas: list[dict[str, Any]] | None = None,
    ) -> bool:
        """Check if the prompt prefix matches the previous request.

        Returns True if this is a cache hit (same prompt prefix).
        """
        current_hash = self.compute_hash(system_prompt, tool_schemas)
        is_hit = current_hash == self._previous_hash and self._previous_hash != ""

        if is_hit:
            self._stats.hits += 1
            self._stats.last_hit_at = time.time()
        else:
            self._stats.misses += 1

        self._stats.last_hash = current_hash
        self._previous_hash = current_hash
        return is_hit

    def invalidate(self) -> None:
        """Force a cache miss on the next check."""
        self._previous_hash = ""

    @property
    def stats(self) -> CacheStats:
        return self._stats

    @property
    def hit_rate(self) -> float:
        """Cache hit rate (0.0 to 1.0)."""
        total = self._stats.hits + self._stats.misses
        if total == 0:
            return 0.0
        return self._stats.hits / total

    def summary(self) -> str:
        """Human-readable cache summary."""
        rate = self.hit_rate * 100
        return f"Prompt cache: {self._stats.hits} hits, {self._stats.misses} misses ({rate:.0f}% hit rate)"
