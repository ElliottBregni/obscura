"""Tests for obscura.core.llm_cache."""

from __future__ import annotations

import time
from unittest.mock import patch

from obscura.core.llm_cache import CacheEntry, CacheStats, LLMCache


class TestCacheEntry:
    def test_not_expired(self) -> None:
        entry = CacheEntry(
            key="k",
            response_text="hi",
            backend="claude",
            model="sonnet",
            created_at=time.monotonic(),
            ttl_seconds=300.0,
        )
        assert entry.is_expired() is False

    def test_expired(self) -> None:
        entry = CacheEntry(
            key="k",
            response_text="hi",
            backend="claude",
            model="sonnet",
            created_at=time.monotonic() - 400.0,
            ttl_seconds=300.0,
        )
        assert entry.is_expired() is True


class TestLLMCacheBasic:
    def test_put_and_get(self) -> None:
        cache = LLMCache()
        cache.put("k1", "hello", backend="claude", model="sonnet")
        entry = cache.get("k1")
        assert entry is not None
        assert entry.response_text == "hello"
        assert entry.backend == "claude"

    def test_get_miss(self) -> None:
        cache = LLMCache()
        assert cache.get("nonexistent") is None

    def test_get_expired(self) -> None:
        cache = LLMCache(default_ttl=0.01)
        cache.put("k1", "hi", backend="b", model="m")
        time.sleep(0.02)
        assert cache.get("k1") is None

    def test_hit_count(self) -> None:
        cache = LLMCache()
        cache.put("k1", "hi", backend="b", model="m")
        cache.get("k1")
        cache.get("k1")
        entry = cache.get("k1")
        assert entry is not None
        assert entry.hit_count == 3

    def test_custom_ttl(self) -> None:
        cache = LLMCache(default_ttl=300.0)
        cache.put("k1", "hi", backend="b", model="m", ttl=0.01)
        time.sleep(0.02)
        assert cache.get("k1") is None


class TestLLMCacheLRU:
    def test_evicts_oldest(self) -> None:
        cache = LLMCache(max_entries=2)
        cache.put("k1", "a", backend="b", model="m")
        cache.put("k2", "b", backend="b", model="m")
        cache.put("k3", "c", backend="b", model="m")  # evicts k1
        assert cache.get("k1") is None
        assert cache.get("k2") is not None
        assert cache.get("k3") is not None

    def test_access_refreshes_lru(self) -> None:
        cache = LLMCache(max_entries=2)
        cache.put("k1", "a", backend="b", model="m")
        cache.put("k2", "b", backend="b", model="m")
        cache.get("k1")  # refresh k1
        cache.put("k3", "c", backend="b", model="m")  # evicts k2 (oldest)
        assert cache.get("k1") is not None
        assert cache.get("k2") is None
        assert cache.get("k3") is not None

    def test_update_existing(self) -> None:
        cache = LLMCache(max_entries=2)
        cache.put("k1", "old", backend="b", model="m")
        cache.put("k1", "new", backend="b", model="m")
        entry = cache.get("k1")
        assert entry is not None
        assert entry.response_text == "new"


class TestLLMCacheInvalidate:
    def test_invalidate_existing(self) -> None:
        cache = LLMCache()
        cache.put("k1", "hi", backend="b", model="m")
        assert cache.invalidate("k1") is True
        assert cache.get("k1") is None

    def test_invalidate_nonexistent(self) -> None:
        cache = LLMCache()
        assert cache.invalidate("nope") is False


class TestLLMCacheClear:
    def test_clear(self) -> None:
        cache = LLMCache()
        cache.put("k1", "a", backend="b", model="m")
        cache.put("k2", "b", backend="b", model="m")
        cache.clear()
        assert cache.get("k1") is None
        assert cache.get("k2") is None


class TestLLMCacheStats:
    def test_initial_stats(self) -> None:
        cache = LLMCache(max_entries=500)
        stats = cache.stats()
        assert stats.hits == 0
        assert stats.misses == 0
        assert stats.evictions == 0
        assert stats.entries == 0
        assert stats.max_entries == 500

    def test_stats_after_operations(self) -> None:
        cache = LLMCache(max_entries=1)
        cache.put("k1", "a", backend="b", model="m")
        cache.get("k1")  # hit
        cache.get("k2")  # miss
        cache.put("k2", "b", backend="b", model="m")  # evicts k1
        stats = cache.stats()
        assert stats.hits == 1
        assert stats.misses == 1
        assert stats.evictions == 1
        assert stats.entries == 1


class TestLLMCacheMakeKey:
    def test_deterministic(self) -> None:
        k1 = LLMCache.make_key("claude", "sonnet", "sys", "prompt")
        k2 = LLMCache.make_key("claude", "sonnet", "sys", "prompt")
        assert k1 == k2

    def test_different_inputs(self) -> None:
        k1 = LLMCache.make_key("claude", "sonnet", "sys", "prompt1")
        k2 = LLMCache.make_key("claude", "sonnet", "sys", "prompt2")
        assert k1 != k2

    def test_hex_format(self) -> None:
        key = LLMCache.make_key("b", "m", "s", "p")
        assert len(key) == 64  # SHA-256 hex digest
        assert all(c in "0123456789abcdef" for c in key)
