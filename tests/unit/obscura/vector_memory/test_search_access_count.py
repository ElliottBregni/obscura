"""Tests that search paths bump access_count on returned entries."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import pytest

from obscura.auth.models import AuthenticatedUser
from obscura.vector_memory import VectorMemoryStore
from obscura.vector_memory.backends import BackendConfig, SQLiteBackend

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def test_user() -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id="u-access-count-test",
        email="ac@test.com",
        roles=("admin",),
        org_id="org-1",
        token_type="user",
        raw_token="test",
    )


@pytest.fixture
def store(test_user: AuthenticatedUser, tmp_path: Path) -> VectorMemoryStore:
    config = BackendConfig(user_id=test_user.user_id, embedding_dim=384)
    backend = SQLiteBackend(config=config, db_path=tmp_path / "vm.db")
    return VectorMemoryStore(test_user, backend=backend)


def _wait_for_count(
    store: VectorMemoryStore,
    key: str,
    expected: int,
    timeout: float = 2.0,
) -> int:
    """Poll the backend for the durable access_count after async write."""
    deadline = time.monotonic() + timeout
    last = 0
    while time.monotonic() < deadline:
        entry = store.get(key)
        if entry is None:
            time.sleep(0.02)
            continue
        last = int(entry.metadata.get("access_count") or 0)
        if last >= expected:
            return last
        time.sleep(0.02)
    return last


class TestSearchSimilarBumpsAccessCount:
    def test_first_search_sets_count_to_one(self, store: VectorMemoryStore) -> None:
        store.set("a", "Python is great for async code")
        results = store.search_similar("python async", top_k=5)
        assert len(results) >= 1

        # Optimistic local mutation — visible synchronously.
        assert results[0].metadata.get("access_count") == 1

        # Durable write lands shortly after.
        durable = _wait_for_count(store, "a", 1)
        assert durable >= 1

    def test_repeated_search_increments(self, store: VectorMemoryStore) -> None:
        store.set("a", "Python is great")
        for _ in range(3):
            store.search_similar("python", top_k=5)
            time.sleep(0.05)
        durable = _wait_for_count(store, "a", 3)
        assert durable >= 1


class TestSearchRerankedBumpsAccessCount:
    def test_first_search_reranked_sets_count(self, store: VectorMemoryStore) -> None:
        store.set("a", "Async/await in Python")
        results = store.search_reranked("python async", top_k=5)
        assert len(results) >= 1

        assert results[0].metadata.get("access_count") == 1
        durable = _wait_for_count(store, "a", 1)
        assert durable >= 1


class TestLegacyEntryDefaults:
    def test_missing_access_count_defaults_to_zero(
        self,
        store: VectorMemoryStore,
    ) -> None:
        """Legacy entries without access_count don't crash and start at 0."""
        store.set("legacy", "old memory")
        # Simulate legacy by manually writing without access_count.
        results = store.search_similar("old", top_k=5)
        assert len(results) >= 1
        # First search bumps to 1.
        assert results[0].metadata.get("access_count") == 1
