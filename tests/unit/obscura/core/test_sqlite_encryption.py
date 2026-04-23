"""Tests for obscura.core.sqlite_encryption.

The encryption backend (sqlcipher3) is optional — these tests exercise
the wrapper's behavior both when it's installed and when it isn't so
both deployment postures are covered.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from obscura.core import sqlite_encryption
from obscura.core.sqlite_encryption import (
    SqlCipherUnavailable,
    _reset_warned_paths,
    is_encryption_available,
    open_connection,
    resolve_db_key,
)


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Point the fallback key file at a temp location so tests don't
    # touch the real ~/.obscura/db.key, and clear env influence.
    monkeypatch.setattr(sqlite_encryption, "_DEFAULT_KEY_FILE", tmp_path / "db.key")
    monkeypatch.delenv("OBSCURA_DB_KEY", raising=False)
    _reset_warned_paths()


# ---------------------------------------------------------------------------
# Backend probing
# ---------------------------------------------------------------------------


def test_is_encryption_available_tracks_module_import() -> None:
    # Either the real package is installed (true) or it isn't (false);
    # the probe should be honest either way.
    assert is_encryption_available() in (True, False)


def test_encryption_unavailable_when_sqlcipher_not_importable() -> None:
    with patch.object(sqlite_encryption, "_probe_sqlcipher", return_value=None):
        assert is_encryption_available() is False


# ---------------------------------------------------------------------------
# Key resolution
# ---------------------------------------------------------------------------


def test_env_var_takes_priority_over_everything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OBSCURA_DB_KEY", "deadbeef")
    assert resolve_db_key() == "deadbeef"


def test_generates_and_persists_key_when_nothing_found(
    tmp_path: Path,
) -> None:
    # No env, no keyring, no file. Must generate + persist + return.
    key_file = tmp_path / "db.key"
    with patch.object(sqlite_encryption, "_read_keyring", return_value=None):
        with patch.object(sqlite_encryption, "_write_keyring", return_value=False):
            with patch.object(sqlite_encryption, "_DEFAULT_KEY_FILE", key_file):
                key = resolve_db_key()
    assert key is not None
    assert len(key) == 64  # 32 bytes hex-encoded
    assert key_file.exists()
    # Must be mode 0o600 on unix.
    if os.name != "nt":
        assert oct(key_file.stat().st_mode)[-3:] == "600"
    assert key_file.read_text() == key


def test_returns_none_when_no_key_and_creation_not_allowed(
    tmp_path: Path,
) -> None:
    with patch.object(sqlite_encryption, "_read_keyring", return_value=None):
        with patch.object(
            sqlite_encryption, "_DEFAULT_KEY_FILE", tmp_path / "db.key"
        ):
            assert resolve_db_key(create_if_missing=False) is None


def test_reads_existing_key_from_file_fallback(tmp_path: Path) -> None:
    key_file = tmp_path / "db.key"
    key_file.write_text("cafef00d")
    with patch.object(sqlite_encryption, "_read_keyring", return_value=None):
        with patch.object(sqlite_encryption, "_DEFAULT_KEY_FILE", key_file):
            assert resolve_db_key() == "cafef00d"


def test_keyring_preferred_over_file(tmp_path: Path) -> None:
    key_file = tmp_path / "db.key"
    key_file.write_text("file-key")
    with patch.object(sqlite_encryption, "_read_keyring", return_value="kr-key"):
        with patch.object(sqlite_encryption, "_DEFAULT_KEY_FILE", key_file):
            assert resolve_db_key() == "kr-key"


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------


def test_open_connection_falls_back_to_sqlite3_when_backend_absent(
    tmp_path: Path,
) -> None:
    db = tmp_path / "unenc.db"
    with patch.object(sqlite_encryption, "_probe_sqlcipher", return_value=None):
        conn = open_connection(db)
    assert isinstance(conn, sqlite3.Connection)
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.commit()
    conn.close()
    # Standard sqlite3 file — should start with the magic header.
    assert db.read_bytes().startswith(b"SQLite format 3")


def test_open_connection_raises_when_encryption_required_but_absent(
    tmp_path: Path,
) -> None:
    db = tmp_path / "must-be-encrypted.db"
    with patch.object(sqlite_encryption, "_probe_sqlcipher", return_value=None):
        with pytest.raises(SqlCipherUnavailable):
            open_connection(db, require_encryption=True)


def test_unencrypted_fallback_warns_once_per_path(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    db = tmp_path / "warn-once.db"
    with patch.object(sqlite_encryption, "_probe_sqlcipher", return_value=None):
        import logging

        with caplog.at_level(logging.WARNING):
            open_connection(db).close()
            open_connection(db).close()
    warnings = [r for r in caplog.records if "UNENCRYPTED" in r.getMessage()]
    assert len(warnings) == 1, f"expected one warning, got {len(warnings)}"


def test_open_connection_uses_sqlcipher_when_present(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With a fake sqlcipher module, the wrapper must open via it and
    apply PRAGMA key + cipher_page_size + kdf_iter + a verification
    read. Failure to do any of these would silently leave the store
    unencrypted in production."""
    db = tmp_path / "enc.db"
    calls: list[str] = []

    class _FakeConn:
        def execute(self, stmt: str, *args: Any) -> "_FakeConn":
            calls.append(stmt)
            return self

        def fetchone(self) -> tuple[int, ...]:
            return (0,)

        def commit(self) -> None: ...
        def close(self) -> None: ...

    class _FakeModule:
        @staticmethod
        def connect(path: str) -> _FakeConn:
            calls.append(f"CONNECT:{path}")
            return _FakeConn()

    monkeypatch.setenv("OBSCURA_DB_KEY", "deadbeef")
    with patch.object(sqlite_encryption, "_probe_sqlcipher", return_value=_FakeModule):
        open_connection(db)

    assert any(c.startswith("CONNECT:") for c in calls)
    assert any('PRAGMA key = "x\'deadbeef\'"' == c for c in calls)
    assert any("cipher_page_size" in c for c in calls)
    assert any("kdf_iter" in c for c in calls)
    assert any("sqlite_master" in c for c in calls), "key must be verified eagerly"
