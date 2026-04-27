"""Tests for the Copilot 403 cache."""

from __future__ import annotations

import time

import pytest

from obscura.auth.copilot_403_cache import (
    Copilot403Cache,
    clear_cache_for_tests,
    is_oauth_token_blocked,
    mark_oauth_token_blocked,
)


@pytest.fixture(autouse=True)
def reset_cache() -> None:
    clear_cache_for_tests()


class TestCopilot403Cache:
    def test_unmarked_tokens_not_blocked(self) -> None:
        cache = Copilot403Cache(ttl_seconds=60)
        assert cache.is_blocked("user-1", "ghp_token") is False

    def test_marked_tokens_are_blocked(self) -> None:
        cache = Copilot403Cache(ttl_seconds=60)
        cache.mark_blocked("user-1", "ghp_token")
        assert cache.is_blocked("user-1", "ghp_token") is True

    def test_blocked_entries_expire(self) -> None:
        cache = Copilot403Cache(ttl_seconds=0)
        cache.mark_blocked("user-1", "ghp_token")
        time.sleep(0.01)
        assert cache.is_blocked("user-1", "ghp_token") is False

    def test_different_users_isolated(self) -> None:
        cache = Copilot403Cache(ttl_seconds=60)
        cache.mark_blocked("user-1", "ghp_token")
        assert cache.is_blocked("user-2", "ghp_token") is False

    def test_token_rotation_clears_block(self) -> None:
        """A new token (different first 8 chars) is not considered blocked."""
        cache = Copilot403Cache(ttl_seconds=60)
        cache.mark_blocked("user-1", "ghp_oldtokenxxx")
        assert cache.is_blocked("user-1", "ghp_newtokenyyy") is False

    def test_empty_inputs_never_block(self) -> None:
        cache = Copilot403Cache(ttl_seconds=60)
        cache.mark_blocked("", "token")
        cache.mark_blocked("user-1", "")
        assert cache.is_blocked("", "token") is False
        assert cache.is_blocked("user-1", "") is False

    def test_clear_drops_all(self) -> None:
        cache = Copilot403Cache(ttl_seconds=60)
        cache.mark_blocked("user-1", "a")
        cache.mark_blocked("user-2", "b")
        assert cache.size() == 2
        cache.clear()
        assert cache.size() == 0

    def test_module_level_helpers_use_singleton(self) -> None:
        mark_oauth_token_blocked("user-1", "ghp_token")
        assert is_oauth_token_blocked("user-1", "ghp_token") is True
