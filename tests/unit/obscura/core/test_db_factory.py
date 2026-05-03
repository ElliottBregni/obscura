"""DatabaseFactory dispatches to the right event store backend.

The strict-typing pass tightened the factory to construct stores
explicitly (no more `**dict` spread) and added a typed
``EventStore`` union return so callers can rely on the result.
"""

from __future__ import annotations

import pytest

from obscura.core.db_factory import DatabaseFactory, get_event_store
from obscura.core.event_store import SQLiteEventStore


def test_create_event_store_defaults_to_sqlite(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OBSCURA_DB_TYPE", raising=False)
    store = DatabaseFactory.create_event_store()
    assert isinstance(store, SQLiteEventStore)


def test_create_event_store_sqlite_explicit() -> None:
    store = DatabaseFactory.create_event_store("sqlite")
    assert isinstance(store, SQLiteEventStore)


def test_unknown_db_type_raises() -> None:
    with pytest.raises(ValueError, match="Unknown database type"):
        DatabaseFactory.create_event_store("redis")


def test_postgres_requires_password(monkeypatch: pytest.MonkeyPatch) -> None:
    """When PG is selected without OBSCURA_PG_PASSWORD we surface a clear error."""
    monkeypatch.setenv("OBSCURA_DB_TYPE", "postgresql")
    monkeypatch.delenv("OBSCURA_PG_PASSWORD", raising=False)

    with pytest.raises(ValueError, match="OBSCURA_PG_PASSWORD"):
        DatabaseFactory.create_event_store("postgresql")


def test_get_event_store_helper_routes_through_factory() -> None:
    store = get_event_store("sqlite")
    assert isinstance(store, SQLiteEventStore)


def test_env_var_drives_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """OBSCURA_DB_TYPE env var picks the backend when no arg is passed."""
    monkeypatch.setenv("OBSCURA_DB_TYPE", "sqlite")
    store = DatabaseFactory.create_event_store()
    assert isinstance(store, SQLiteEventStore)
