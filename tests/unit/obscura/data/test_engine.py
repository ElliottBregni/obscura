"""Tests for the data-layer engine: backend resolution + connections."""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from obscura.data.engine import (
    Backend,
    DataLayerError,
    _parse_db_url,
    resolve_backend,
    sqlite_connection,
    sqlite_path,
)


@pytest.mark.unit
class TestResolveBackend:
    def test_default_is_sqlite(self) -> None:
        with patch.dict(
            "os.environ",
            {},
            clear=False,
        ) as _env:
            for key in ("OBSCURA_DB_URL", "OBSCURA_PG_HOST", "OBSCURA_PG_PASSWORD"):
                _env.pop(key, None)
            assert resolve_backend() == Backend.SQLITE

    def test_db_url_postgres_wins(self) -> None:
        with patch.dict(
            "os.environ",
            {"OBSCURA_DB_URL": "postgresql://u:p@h:5432/d"},
            clear=False,
        ):
            assert resolve_backend() == Backend.POSTGRES

    def test_db_url_postgresql_dialect_wins(self) -> None:
        with patch.dict(
            "os.environ",
            {"OBSCURA_DB_URL": "postgresql+psycopg://u:p@h/d"},
            clear=False,
        ):
            assert resolve_backend() == Backend.POSTGRES

    def test_db_url_sqlite(self) -> None:
        with patch.dict(
            "os.environ",
            {"OBSCURA_DB_URL": "sqlite:///tmp/foo"},
            clear=False,
        ):
            assert resolve_backend() == Backend.SQLITE

    def test_legacy_pg_env_vars_select_postgres(self) -> None:
        with patch.dict(
            "os.environ",
            {"OBSCURA_PG_HOST": "db.example.com", "OBSCURA_PG_PASSWORD": "x"},
            clear=False,
        ) as _env:
            _env.pop("OBSCURA_DB_URL", None)
            assert resolve_backend() == Backend.POSTGRES

    def test_db_url_overrides_legacy(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "OBSCURA_DB_URL": "sqlite:///tmp/x",
                "OBSCURA_PG_HOST": "db.example.com",
            },
            clear=False,
        ):
            assert resolve_backend() == Backend.SQLITE

    def test_unknown_scheme_fails_loud(self) -> None:
        with pytest.raises(DataLayerError, match="Unsupported"):
            _parse_db_url("mongodb://localhost")


@pytest.mark.unit
class TestSqlitePath:
    def test_default_under_obscura_home(self) -> None:
        with patch.dict("os.environ", {}, clear=False) as _env:
            _env.pop("OBSCURA_DB_URL", None)
            p = sqlite_path("memories")
            assert p.name == "memories.db"
            assert p.parent.name == ".obscura"

    def test_db_url_directory_honoured(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with patch.dict(
                "os.environ",
                {"OBSCURA_DB_URL": f"sqlite://{td}"},
                clear=False,
            ):
                p = sqlite_path("events")
                assert p == Path(td) / "events.db"
                assert p.parent.is_dir()


@pytest.mark.unit
class TestSqliteConnection:
    def test_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with patch.dict(
                "os.environ",
                {"OBSCURA_DB_URL": f"sqlite://{td}"},
                clear=False,
            ):
                with sqlite_connection("test_store") as conn:
                    conn.execute(
                        "CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)",
                    )
                    conn.execute("INSERT INTO t (v) VALUES (?)", ("hello",))
                    conn.commit()

                with sqlite_connection("test_store") as conn:
                    rows = conn.execute("SELECT v FROM t").fetchall()
                    assert len(rows) == 1
                    assert rows[0]["v"] == "hello"

    def test_returns_row_factory(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with patch.dict(
                "os.environ",
                {"OBSCURA_DB_URL": f"sqlite://{td}"},
                clear=False,
            ):
                with sqlite_connection("rf") as conn:
                    assert conn.row_factory is sqlite3.Row


@pytest.mark.unit
class TestPostgresPool:
    def test_no_password_fails_loud(self) -> None:
        from obscura.core.pg_config import PGPoolManager

        from obscura.data.engine import get_postgres_pool

        with patch.dict(
            "os.environ",
            {"OBSCURA_PG_HOST": "nowhere.example.com"},
            clear=False,
        ) as _env:
            _env.pop("OBSCURA_PG_PASSWORD", None)
            _env.pop("OBSCURA_DB_URL", None)
            PGPoolManager._pool = None  # pyright: ignore[reportPrivateUsage]
            PGPoolManager._config = None  # pyright: ignore[reportPrivateUsage]
            with pytest.raises(DataLayerError, match="Postgres pool unavailable"):
                get_postgres_pool()

    def test_db_url_populates_pg_env(self) -> None:
        from obscura.data.engine import _apply_url_to_pg_env

        with patch.dict("os.environ", {}, clear=False) as _env:
            for key in (
                "OBSCURA_PG_HOST",
                "OBSCURA_PG_PORT",
                "OBSCURA_PG_USER",
                "OBSCURA_PG_PASSWORD",
                "OBSCURA_PG_DATABASE",
            ):
                _env.pop(key, None)
            _apply_url_to_pg_env(
                "postgresql://alice:secret@db.example.com:6543/obs_test",
            )
            import os

            assert os.environ["OBSCURA_PG_HOST"] == "db.example.com"
            assert os.environ["OBSCURA_PG_PORT"] == "6543"
            assert os.environ["OBSCURA_PG_USER"] == "alice"
            assert os.environ["OBSCURA_PG_PASSWORD"] == "secret"
            assert os.environ["OBSCURA_PG_DATABASE"] == "obs_test"
