"""Tests for CLI Supabase auth commands and credential persistence."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from click.testing import CliRunner

import obscura.cli.auth_commands as auth_commands
from obscura.cli.auth_commands import (
    StoredSession,
    SupabaseCliConfig,
    auth_group,
    clear_session,
    get_access_token,
    load_session,
    save_session,
)


@pytest.fixture
def isolated_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect CREDENTIALS_PATH to a temp file for each test."""
    creds = tmp_path / "credentials.json"
    monkeypatch.setattr(auth_commands, "CREDENTIALS_PATH", creds)
    return creds


def _sample_session(**overrides: Any) -> StoredSession:
    base = {
        "access_token": "access-xxx",
        "refresh_token": "refresh-yyy",
        "expires_at": int(time.time()) + 3600,
        "user_id": "user-1",
        "email": "user@example.com",
        "provider": "github",
    }
    base.update(overrides)
    return StoredSession(**base)  # type: ignore[arg-type]


class TestPersistence:
    def test_save_and_load_roundtrip(self, isolated_credentials: Path) -> None:
        session = _sample_session()
        save_session(session)
        loaded = load_session()
        assert loaded is not None
        assert loaded.access_token == "access-xxx"
        assert loaded.provider == "github"

    def test_load_returns_none_when_missing(self, isolated_credentials: Path) -> None:
        assert load_session() is None

    def test_load_returns_none_on_corrupt_file(
        self, isolated_credentials: Path,
    ) -> None:
        isolated_credentials.parent.mkdir(parents=True, exist_ok=True)
        isolated_credentials.write_text("{not json")
        assert load_session() is None

    def test_clear_session_removes_file(self, isolated_credentials: Path) -> None:
        save_session(_sample_session())
        assert clear_session() is True
        assert not isolated_credentials.exists()
        # Second call is a no-op.
        assert clear_session() is False

    def test_file_written_with_restrictive_perms(
        self, isolated_credentials: Path,
    ) -> None:
        save_session(_sample_session())
        mode = isolated_credentials.stat().st_mode & 0o777
        assert mode == 0o600


class TestGetAccessToken:
    def test_returns_current_token_when_fresh(
        self, isolated_credentials: Path,
    ) -> None:
        save_session(_sample_session(expires_at=int(time.time()) + 3600))
        assert get_access_token() == "access-xxx"

    def test_returns_none_when_no_session(
        self, isolated_credentials: Path,
    ) -> None:
        assert get_access_token() is None

    def test_refreshes_when_expired(
        self,
        isolated_credentials: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        save_session(_sample_session(expires_at=int(time.time()) - 10))
        monkeypatch.setenv("SUPABASE_URL", "https://proj.supabase.co")
        monkeypatch.setenv("SUPABASE_ANON_KEY", "anon-key")

        fresh = _sample_session(
            access_token="fresh-access",
            refresh_token="fresh-refresh",
            expires_at=int(time.time()) + 3600,
            provider="refresh",
        )
        with patch.object(auth_commands, "_refresh_session", return_value=fresh):
            token = get_access_token()

        assert token == "fresh-access"
        stored = load_session()
        assert stored is not None
        # Provider label should be preserved from the original session.
        assert stored.provider == "github"
        assert stored.refresh_token == "fresh-refresh"

    def test_returns_none_when_refresh_fails(
        self,
        isolated_credentials: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        save_session(_sample_session(expires_at=int(time.time()) - 10))
        monkeypatch.setenv("SUPABASE_URL", "https://proj.supabase.co")
        monkeypatch.setenv("SUPABASE_ANON_KEY", "anon-key")

        with patch.object(
            auth_commands,
            "_refresh_session",
            side_effect=RuntimeError("boom"),
        ):
            assert get_access_token() is None


class TestSupabaseCliConfig:
    def test_from_env_returns_none_when_unset(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("SUPABASE_URL", raising=False)
        monkeypatch.delenv("SUPABASE_ANON_KEY", raising=False)
        assert SupabaseCliConfig.from_env() is None

    def test_from_env_strips_trailing_slash(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("SUPABASE_URL", "https://proj.supabase.co/")
        monkeypatch.setenv("SUPABASE_ANON_KEY", "anon")
        cfg = SupabaseCliConfig.from_env()
        assert cfg is not None
        assert cfg.url == "https://proj.supabase.co"


class TestCliCommands:
    def test_whoami_reports_not_signed_in(
        self, isolated_credentials: Path,
    ) -> None:
        runner = CliRunner()
        result = runner.invoke(auth_group, ["whoami"])
        assert result.exit_code == 1
        assert "Not signed in" in result.output

    def test_whoami_reports_session(
        self, isolated_credentials: Path,
    ) -> None:
        save_session(_sample_session())
        runner = CliRunner()
        result = runner.invoke(auth_group, ["whoami"])
        assert result.exit_code == 0
        assert "user@example.com" in result.output
        assert "github" in result.output

    def test_logout_removes_file(
        self, isolated_credentials: Path,
    ) -> None:
        save_session(_sample_session())
        runner = CliRunner()
        result = runner.invoke(auth_group, ["logout"])
        assert result.exit_code == 0
        assert not isolated_credentials.exists()

    def test_login_without_config_fails_cleanly(
        self,
        isolated_credentials: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("SUPABASE_URL", raising=False)
        monkeypatch.delenv("SUPABASE_ANON_KEY", raising=False)
        runner = CliRunner()
        result = runner.invoke(auth_group, ["login", "--provider", "github"])
        assert result.exit_code != 0
        assert "Supabase is not configured" in result.output


class TestStoredSessionSerialization:
    def test_from_dict_accepts_json_roundtrip(self) -> None:
        session = _sample_session()
        payload = json.dumps(session.to_dict())
        loaded = StoredSession.from_dict(json.loads(payload))
        assert loaded == session
