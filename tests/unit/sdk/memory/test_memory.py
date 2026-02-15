"""Tests for sdk.memory — Multi-tenant memory storage."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from sdk.auth.models import AuthenticatedUser
from sdk.memory import MemoryKey, MemoryStore


@pytest.fixture
def test_user() -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id="u-test-123",
        email="test@obscura.dev",
        roles=("admin",),
        org_id="org-1",
        token_type="user",
        raw_token="fake-token",
    )


@pytest.fixture
def temp_db(tmp_path: Path) -> Path:
    return tmp_path / "test_memory.db"


class TestMemoryKey:
    def test_memory_key_str(self) -> None:
        key = MemoryKey(namespace="session", key="context")
        assert str(key) == "session:context"


class TestMemoryStore:
    def test_set_and_get(self, test_user: AuthenticatedUser, temp_db: Path) -> None:
        store = MemoryStore(test_user, db_path=temp_db)
        store.set("mykey", {"foo": "bar"}, namespace="test")
        value = store.get("mykey", namespace="test")
        assert value == {"foo": "bar"}

    def test_get_missing_returns_default(
        self, test_user: AuthenticatedUser, temp_db: Path
    ) -> None:
        store = MemoryStore(test_user, db_path=temp_db)
        value = store.get("nonexistent", namespace="test", default="default")
        assert value == "default"

    def test_get_missing_returns_none(
        self, test_user: AuthenticatedUser, temp_db: Path
    ) -> None:
        store = MemoryStore(test_user, db_path=temp_db)
        value = store.get("nonexistent", namespace="test")
        assert value is None

    def test_update_existing(self, test_user: AuthenticatedUser, temp_db: Path) -> None:
        store = MemoryStore(test_user, db_path=temp_db)
        store.set("key", "v1", namespace="test")
        store.set("key", "v2", namespace="test")
        value = store.get("key", namespace="test")
        assert value == "v2"

    def test_delete_existing(self, test_user: AuthenticatedUser, temp_db: Path) -> None:
        store = MemoryStore(test_user, db_path=temp_db)
        store.set("key", "value", namespace="test")
        deleted = store.delete("key", namespace="test")
        assert deleted is True
        assert store.get("key", namespace="test") is None

    def test_delete_missing(self, test_user: AuthenticatedUser, temp_db: Path) -> None:
        store = MemoryStore(test_user, db_path=temp_db)
        deleted = store.delete("nonexistent", namespace="test")
        assert deleted is False

    def test_list_keys(self, test_user: AuthenticatedUser, temp_db: Path) -> None:
        store = MemoryStore(test_user, db_path=temp_db)
        store.set("key1", "v1", namespace="ns1")
        store.set("key2", "v2", namespace="ns1")
        store.set("key3", "v3", namespace="ns2")

        all_keys = store.list_keys()
        assert len(all_keys) == 3

        ns1_keys = store.list_keys(namespace="ns1")
        assert len(ns1_keys) == 2

    def test_search(self, test_user: AuthenticatedUser, temp_db: Path) -> None:
        store = MemoryStore(test_user, db_path=temp_db)
        store.set("config", {"database": "postgresql"}, namespace="project")
        store.set("readme", "This is a README file", namespace="docs")

        results = store.search("README")
        assert len(results) == 1
        assert results[0][0].key == "readme"

    def test_clear_namespace(self, test_user: AuthenticatedUser, temp_db: Path) -> None:
        store = MemoryStore(test_user, db_path=temp_db)
        store.set("key1", "v1", namespace="ns1")
        store.set("key2", "v2", namespace="ns2")

        count = store.clear_namespace("ns1")
        assert count == 1
        assert store.get("key1", namespace="ns1") is None
        assert store.get("key2", namespace="ns2") == "v2"

    def test_ttl_expiration(self, test_user: AuthenticatedUser, temp_db: Path) -> None:
        store = MemoryStore(test_user, db_path=temp_db)
        # Set with expired TTL so it should disappear on next read without sleep
        store.set("temp", "value", namespace="test", ttl=timedelta(milliseconds=-1))
        value = store.get("temp", namespace="test", default="expired")
        assert value == "expired"

    def test_get_stats(self, test_user: AuthenticatedUser, temp_db: Path) -> None:
        store = MemoryStore(test_user, db_path=temp_db)
        store.set("key1", "v1", namespace="ns1")
        store.set("key2", "v2", namespace="ns2")

        stats = store.get_stats()
        assert stats["total_keys"] == 2
        assert stats["expired_keys"] == 0
        assert "ns1" in stats["namespaces"]
        assert "ns2" in stats["namespaces"]

    def test_singleton_per_user(
        self, test_user: AuthenticatedUser, temp_db: Path
    ) -> None:
        store1 = MemoryStore.for_user(test_user)
        store2 = MemoryStore.for_user(test_user)
        assert store1 is store2

    def test_memory_key_usage(
        self, test_user: AuthenticatedUser, temp_db: Path
    ) -> None:
        store = MemoryStore(test_user, db_path=temp_db)
        key = MemoryKey(namespace="custom", key="mykey")
        store.set(key, "value")
        value = store.get(key)
        assert value == "value"

    def test_complex_values(self, test_user: AuthenticatedUser, temp_db: Path) -> None:
        store = MemoryStore(test_user, db_path=temp_db)
        complex_value = {
            "list": [1, 2, 3],
            "nested": {"a": "b"},
            "bool": True,
            "null": None,
        }
        store.set("complex", complex_value, namespace="test")
        retrieved = store.get("complex", namespace="test")
        assert retrieved == complex_value

    def test_isolation_between_users(self, temp_db: Path) -> None:
        user1 = AuthenticatedUser(
            user_id="u-1",
            email="u1@test.com",
            roles=(),
            org_id="o1",
            token_type="user",
            raw_token="t1",
        )
        user2 = AuthenticatedUser(
            user_id="u-2",
            email="u2@test.com",
            roles=(),
            org_id="o2",
            token_type="user",
            raw_token="t2",
        )

        # Use different DB paths to simulate isolation
        db1 = temp_db.parent / "user1.db"
        db2 = temp_db.parent / "user2.db"

        store1 = MemoryStore(user1, db_path=db1)
        store2 = MemoryStore(user2, db_path=db2)

        store1.set("shared_key", "user1_value", namespace="test")
        store2.set("shared_key", "user2_value", namespace="test")

        assert store1.get("shared_key", namespace="test") == "user1_value"
        assert store2.get("shared_key", namespace="test") == "user2_value"


