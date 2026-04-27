"""Tests for per-profile storage scoping in ``obscura.cli.session``.

The browser extension's native host runs one process per Chrome profile
and forwards a stable ``profile_id`` to ``SessionConfig``. The session
must route ``events.db`` and the SQLite vector-memory directory under
``~/.obscura/profiles/<profile_id>/`` so concurrent profiles cannot
corrupt each other's SQLite session ids.

These tests exercise the pure helpers without spinning up a full
``ObscuraSession`` (which would require a backend).
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from obscura.cli.session import SessionConfig, _resolve_profile_home


class TestSessionConfig:
    def test_profile_id_defaults_to_none(self) -> None:
        """Unspecified profile_id keeps legacy behaviour (terminal REPL)."""
        cfg = SessionConfig()
        assert cfg.profile_id is None

    def test_profile_id_round_trips(self) -> None:
        cfg = SessionConfig(profile_id="abc-123")
        assert cfg.profile_id == "abc-123"


class TestResolveProfileHome:
    def test_none_returns_legacy_home(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``profile_id=None`` must resolve to ``~/.obscura`` exactly.

        This is the contract that keeps the terminal REPL untouched.
        """
        from obscura.cli import session as session_mod

        fake_home = Path("/tmp/obscura-test-home")
        # ``resolve_obscura_home`` is imported by name into the session
        # module, so we patch that bound reference, not the source module.
        monkeypatch.setattr(session_mod, "resolve_obscura_home", lambda: fake_home)

        result = _resolve_profile_home(None)
        assert result == fake_home

    def test_profile_id_creates_subdir_path(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A profile id is appended under ``profiles/`` for isolation."""
        from obscura.cli import session as session_mod

        fake_home = Path("/tmp/obscura-test-home")
        monkeypatch.setattr(session_mod, "resolve_obscura_home", lambda: fake_home)

        result = _resolve_profile_home("chrome-profile-uuid-1")
        assert result == fake_home / "profiles" / "chrome-profile-uuid-1"

    def test_does_not_create_directory(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The helper is pure path math; it must not touch the filesystem.

        Storage classes (``SQLiteEventStore``, the vector backend) own
        directory creation on first write.
        """
        from obscura.cli import session as session_mod

        monkeypatch.setattr(session_mod, "resolve_obscura_home", lambda: tmp_path)

        result = _resolve_profile_home("p1")
        assert not result.exists()


class TestVectorMemoryEnvOverride:
    """``_init_vector_memory`` sets ``OBSCURA_VECTOR_MEMORY_DIR`` when a
    profile_id is set, but never clobbers a caller-supplied override.
    """

    def test_env_var_set_when_profile_supplied(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from obscura.cli import session as session_mod

        # Stub init_vector_store so we don't pull in the embedding model.
        monkeypatch.setattr(session_mod, "init_vector_store", lambda _user: None)
        monkeypatch.delenv("OBSCURA_VECTOR_MEMORY_DIR", raising=False)
        monkeypatch.setenv("USER", "tester")

        # Build a thin stand-in for the bound state ``_init_vector_memory``
        # reads (``_config``, ``_profile_home``).
        sess = session_mod.ObscuraSession.__new__(session_mod.ObscuraSession)
        sess._config = SessionConfig(profile_id="prof-A")
        sess._profile_home = tmp_path / "profiles" / "prof-A"

        sess._init_vector_memory()

        assert os.environ.get("OBSCURA_VECTOR_MEMORY_DIR") == str(
            tmp_path / "profiles" / "prof-A" / "vector_memory",
        )

    def test_env_var_untouched_when_caller_overrode(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An explicit caller override (e.g. CI, test harness) wins."""
        from obscura.cli import session as session_mod

        monkeypatch.setattr(session_mod, "init_vector_store", lambda _user: None)
        monkeypatch.setenv("OBSCURA_VECTOR_MEMORY_DIR", "/explicit/override")
        monkeypatch.setenv("USER", "tester")

        sess = session_mod.ObscuraSession.__new__(session_mod.ObscuraSession)
        sess._config = SessionConfig(profile_id="prof-B")
        sess._profile_home = tmp_path / "profiles" / "prof-B"

        sess._init_vector_memory()

        assert os.environ.get("OBSCURA_VECTOR_MEMORY_DIR") == "/explicit/override"

    def test_no_env_change_without_profile(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Terminal REPL (``profile_id=None``) leaves the env var alone."""
        from obscura.cli import session as session_mod

        monkeypatch.setattr(session_mod, "init_vector_store", lambda _user: None)
        monkeypatch.delenv("OBSCURA_VECTOR_MEMORY_DIR", raising=False)
        monkeypatch.setenv("USER", "tester")

        sess = session_mod.ObscuraSession.__new__(session_mod.ObscuraSession)
        sess._config = SessionConfig(profile_id=None)
        sess._profile_home = tmp_path  # legacy home

        sess._init_vector_memory()

        assert "OBSCURA_VECTOR_MEMORY_DIR" not in os.environ
