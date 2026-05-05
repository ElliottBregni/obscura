"""Tests for the key-value memory repository wrapper."""

from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from obscura.data.memory import (
    MemoryEntry,
    MemoryKey,
    MemoryStore,
    get_memory_store,
)


class _FakeUser:
    """Stand-in for AuthenticatedUser — only ``user_id`` is consumed."""

    def __init__(self, user_id: str = "elliott") -> None:
        self.user_id = user_id


@pytest.fixture()
def isolated_memory(tmp_path: Path) -> Any:
    """Force MemoryStore to use a tmp dir + reset the singleton cache."""
    from obscura.memory.store import MemoryStore as _SqliteMemoryStore

    _SqliteMemoryStore.reset_instances()
    with patch.dict(
        os.environ,
        {"OBSCURA_MEMORY_DIR": str(tmp_path / "memory")},
        clear=False,
    ) as _env:
        # Ensure Postgres path is not selected
        for key in ("OBSCURA_DB_TYPE", "OBSCURA_DB_URL", "OBSCURA_PG_HOST"):
            _env.pop(key, None)
        yield


@pytest.mark.unit
class TestMemoryStoreFactory:
    def test_factory_returns_protocol_conformant(
        self,
        isolated_memory: Any,
    ) -> None:
        del isolated_memory
        store = get_memory_store(_FakeUser())
        assert isinstance(store, MemoryStore)

    def test_factory_returns_sqlite_by_default(
        self,
        isolated_memory: Any,
    ) -> None:
        del isolated_memory
        from obscura.memory.store import MemoryStore as _Sqlite

        store = get_memory_store(_FakeUser())
        assert isinstance(store, _Sqlite)

    def test_factory_returns_postgres_when_legacy_env(
        self,
        isolated_memory: Any,
    ) -> None:
        del isolated_memory
        # Patch is_pg_configured to True so the factory picks Postgres
        # without us actually instantiating one (which would need a DB).
        with patch("obscura.data.memory.factory.is_pg_configured", return_value=True):
            with patch(
                "obscura.data.memory.factory.PostgreSQLMemoryStore.for_user",
                return_value=object(),  # opaque sentinel
            ) as mock_for_user:
                store = get_memory_store(_FakeUser())
                assert mock_for_user.called
                assert store is not None


@pytest.mark.unit
class TestMemoryStoreLifecycle:
    """Smoke-test the SQLite path end-to-end through the factory."""

    def test_set_get_round_trip(self, isolated_memory: Any) -> None:
        del isolated_memory
        store = get_memory_store(_FakeUser("alice"))
        store.set("foo", {"value": 42})
        assert store.get("foo") == {"value": 42}

    def test_delete(self, isolated_memory: Any) -> None:
        del isolated_memory
        store = get_memory_store(_FakeUser("bob"))
        store.set("temp", "scratch")
        assert store.delete("temp") is True
        assert store.get("temp") is None
        assert store.delete("temp") is False  # already gone

    def test_namespace_isolation(self, isolated_memory: Any) -> None:
        del isolated_memory
        store = get_memory_store(_FakeUser("carol"))
        store.set("k", "in default")
        store.set("k", "in project", namespace="project:obscura")
        assert store.get("k") == "in default"
        assert store.get("k", namespace="project:obscura") == "in project"

    def test_ttl_expiry(self, isolated_memory: Any) -> None:
        del isolated_memory
        store = get_memory_store(_FakeUser("dave"))
        store.set("ephemeral", "gone in a flash", ttl=timedelta(microseconds=1))
        # Even a microsecond-old TTL should be considered expired by now.
        # The legacy semantics: get returns None for expired entries.
        import time

        time.sleep(0.01)
        assert store.get("ephemeral") is None

    def test_clear_namespace(self, isolated_memory: Any) -> None:
        del isolated_memory
        store = get_memory_store(_FakeUser("eve"))
        store.set("a", 1, namespace="scratch")
        store.set("b", 2, namespace="scratch")
        store.set("c", 3, namespace="keep")
        removed = store.clear_namespace("scratch")
        assert removed == 2
        assert store.get("a", namespace="scratch") is None
        assert store.get("c", namespace="keep") == 3

    def test_list_keys(self, isolated_memory: Any) -> None:
        del isolated_memory
        store = get_memory_store(_FakeUser("frank"))
        store.set("k1", 1)
        store.set("k2", 2)
        store.set("k3", 3, namespace="other")
        all_keys = store.list_keys()
        assert len(all_keys) == 3
        default_only = store.list_keys(namespace="default")
        assert len(default_only) == 2

    def test_get_stats(self, isolated_memory: Any) -> None:
        del isolated_memory
        store = get_memory_store(_FakeUser("grace"))
        store.set("a", 1)
        store.set("b", 2)
        stats = store.get_stats()
        assert "total" in stats or "namespaces" in stats or stats


@pytest.mark.unit
class TestProtocolConformance:
    def test_postgres_class_has_required_methods(self) -> None:
        from obscura.memory.postgres_memory import PostgreSQLMemoryStore

        for method in (
            "set",
            "get",
            "delete",
            "list_keys",
            "search",
            "clear_namespace",
            "clear_expired",
            "get_stats",
            "close",
        ):
            assert hasattr(PostgreSQLMemoryStore, method), method


@pytest.mark.unit
class TestMemoryKeyEntry:
    def test_memory_key_construction(self) -> None:
        k = MemoryKey(namespace="user:profile", key="elliott")
        assert k.namespace == "user:profile"
        assert k.key == "elliott"

    def test_memory_entry_construction(self) -> None:
        from datetime import UTC, datetime

        now = datetime.now(UTC)
        e = MemoryEntry(
            key=MemoryKey(namespace="default", key="foo"),
            value={"hello": "world"},
            created_at=now,
            updated_at=now,
        )
        assert e.key.key == "foo"
        assert e.value == {"hello": "world"}
        assert e.is_expired is False
