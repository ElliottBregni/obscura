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
    ensure_github_oauth_session,
    get_access_token,
    get_github_token,
    load_session,
    save_session,
)


@pytest.fixture(autouse=True)
def _skip_dotenv_autoload(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable .env auto-loading — tests manage env vars explicitly and
    don't want the repo's real SUPABASE_* leaking in via _load_dotenv_best_effort.
    """
    monkeypatch.setattr(auth_commands, "_load_dotenv_best_effort", lambda: None)


@pytest.fixture
def isolated_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate session storage to a temp file + disable the real OS keychain.

    Otherwise tests that leak into the user's keychain pass once but fail on
    re-runs because `load_session` finds stale entries from a prior run.
    """
    creds = tmp_path / "credentials.json"
    monkeypatch.setattr(auth_commands, "CREDENTIALS_PATH", creds)
    monkeypatch.setattr(auth_commands, "_keyring_available", lambda: False)
    return creds


def _sample_session(**overrides: Any) -> StoredSession:
    base: dict[str, Any] = {
        "access_token": "access-xxx",
        "refresh_token": "refresh-yyy",
        "expires_at": int(time.time()) + 3600,
        "user_id": "user-1",
        "email": "user@example.com",
        "provider": "github",
    }
    base.update(overrides)
    return StoredSession(**base)


class TestPersistence:
    def test_save_and_load_roundtrip(self, isolated_credentials: Path) -> None:
        _ = isolated_credentials
        session = _sample_session()
        save_session(session)
        loaded = load_session()
        assert loaded is not None
        assert loaded.access_token == "access-xxx"
        assert loaded.provider == "github"

    def test_load_returns_none_when_missing(self, isolated_credentials: Path) -> None:
        _ = isolated_credentials
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
        _ = isolated_credentials
        save_session(_sample_session(expires_at=int(time.time()) + 3600))
        assert get_access_token() == "access-xxx"

    def test_returns_none_when_no_session(self, isolated_credentials: Path) -> None:
        _ = isolated_credentials
        assert get_access_token() is None

    def test_refreshes_when_expired(
        self,
        isolated_credentials: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _ = isolated_credentials
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
        assert stored.provider == "github"  # preserved from original
        assert stored.refresh_token == "fresh-refresh"

    def test_returns_none_when_refresh_fails(
        self,
        isolated_credentials: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _ = isolated_credentials
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
    def test_whoami_reports_not_signed_in(self, isolated_credentials: Path) -> None:
        _ = isolated_credentials
        result = CliRunner().invoke(auth_group, ["whoami"])
        assert result.exit_code == 1
        assert "Not signed in" in result.output

    def test_whoami_reports_session(self, isolated_credentials: Path) -> None:
        _ = isolated_credentials
        save_session(_sample_session())
        result = CliRunner().invoke(auth_group, ["whoami"])
        assert result.exit_code == 0
        assert "user@example.com" in result.output
        assert "github" in result.output

    def test_logout_removes_file(self, isolated_credentials: Path) -> None:
        save_session(_sample_session())
        result = CliRunner().invoke(auth_group, ["logout"])
        assert result.exit_code == 0
        assert not isolated_credentials.exists()

    def test_login_without_config_fails_cleanly(
        self,
        isolated_credentials: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _ = isolated_credentials
        monkeypatch.delenv("SUPABASE_URL", raising=False)
        monkeypatch.delenv("SUPABASE_ANON_KEY", raising=False)
        result = CliRunner().invoke(auth_group, ["login", "--provider", "github"])
        assert result.exit_code != 0
        assert "Supabase is not configured" in result.output


class TestEnsureGithubOauthSession:
    def test_returns_none_when_supabase_unconfigured(
        self, isolated_credentials: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _ = isolated_credentials
        monkeypatch.delenv("SUPABASE_URL", raising=False)
        monkeypatch.delenv("SUPABASE_ANON_KEY", raising=False)

        with patch.object(auth_commands, "_run_oauth_flow") as oauth_flow:
            assert ensure_github_oauth_session(open_browser=False) is None
            oauth_flow.assert_not_called()

    def test_reuses_existing_valid_session(
        self, isolated_credentials: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _ = isolated_credentials
        save_session(_sample_session(expires_at=int(time.time()) + 3600))
        monkeypatch.setenv("SUPABASE_URL", "https://proj.supabase.co")
        monkeypatch.setenv("SUPABASE_ANON_KEY", "anon-key")

        with patch.object(auth_commands, "_run_oauth_flow") as oauth_flow:
            session = ensure_github_oauth_session(open_browser=False)
            assert session is not None
            assert session.email == "user@example.com"
            oauth_flow.assert_not_called()

    def test_runs_oauth_when_session_missing(
        self, isolated_credentials: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _ = isolated_credentials
        monkeypatch.setenv("SUPABASE_URL", "https://proj.supabase.co")
        monkeypatch.setenv("SUPABASE_ANON_KEY", "anon-key")

        fresh = _sample_session(email="new@example.com", user_id="new-user")
        with patch.object(auth_commands, "_run_oauth_flow", return_value=fresh) as oauth_flow:
            session = ensure_github_oauth_session(open_browser=False)
            assert session is not None
            assert session.email == "new@example.com"
            oauth_flow.assert_called_once()


class TestStoredSessionSerialization:
    def test_from_dict_accepts_json_roundtrip(self) -> None:
        session = _sample_session()
        payload = json.dumps(session.to_dict())
        loaded = StoredSession.from_dict(json.loads(payload))
        assert loaded == session

    def test_from_dict_without_provider_token_field(self) -> None:
        """Older sessions (before provider_token was added) still load."""
        raw = {
            "access_token": "a",
            "refresh_token": "r",
            "expires_at": 1,
            "user_id": "u",
            "email": "e",
            "provider": "github",
            # no provider_token key
        }
        loaded = StoredSession.from_dict(raw)
        assert loaded.provider_token is None
        assert loaded.provider_refresh_token is None


class TestProviderSecretMetadata:
    def test_build_provider_secrets_metadata_merges_existing(self) -> None:
        existing = {
            "name": "User",
            "obscura_provider_secrets": {
                "google": {"provider_token": "goog-token"},
                "github": {"provider_token": "old-gh"},
            },
        }
        session = _sample_session(
            provider_token="new-gh-token",
            provider_refresh_token="new-gh-refresh",
        )

        merged = auth_commands._build_provider_secrets_metadata(
            existing_user_metadata=existing,
            provider="github",
            session=session,
        )

        assert merged["name"] == "User"
        secrets = merged["obscura_provider_secrets"]
        assert secrets["google"]["provider_token"] == "goog-token"
        assert secrets["github"]["provider_token"] == "new-gh-token"
        assert secrets["github"]["provider_refresh_token"] == "new-gh-refresh"

    def test_build_provider_secrets_metadata_no_secrets_noop(self) -> None:
        existing = {"name": "User"}
        session = _sample_session(provider_token=None, provider_refresh_token=None)

        merged = auth_commands._build_provider_secrets_metadata(
            existing_user_metadata=existing,
            provider="github",
            session=session,
        )

        assert merged == existing


class TestGithubTokenAccessor:
    def test_returns_none_when_no_session(
        self, isolated_credentials: Path,
    ) -> None:
        _ = isolated_credentials
        assert get_github_token() is None

    def test_returns_provider_token_when_set(
        self, isolated_credentials: Path,
    ) -> None:
        _ = isolated_credentials
        save_session(_sample_session(provider_token="ghp_fake123"))
        assert get_github_token() == "ghp_fake123"

    def test_returns_none_when_session_has_no_provider_token(
        self, isolated_credentials: Path,
    ) -> None:
        """Magic-link sessions don't have provider tokens."""
        _ = isolated_credentials
        save_session(_sample_session(provider="magic", provider_token=None))
        assert get_github_token() is None

    def test_refresh_preserves_provider_token_when_not_reissued(
        self,
        isolated_credentials: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Supabase's refresh endpoint may omit provider_token — preserve the old one."""
        _ = isolated_credentials
        import obscura.cli.auth_commands as auth_commands

        save_session(
            _sample_session(
                expires_at=int(time.time()) - 10,
                provider_token="ghp_original",
            ),
        )
        monkeypatch.setenv("SUPABASE_URL", "https://proj.supabase.co")
        monkeypatch.setenv("SUPABASE_ANON_KEY", "anon-key")

        refreshed = _sample_session(
            access_token="fresh-access",
            refresh_token="fresh-refresh",
            expires_at=int(time.time()) + 3600,
            provider="refresh",
            provider_token=None,  # Supabase didn't re-issue
            provider_refresh_token=None,
        )
        with (
            patch.object(auth_commands, "_refresh_session", return_value=refreshed),
            patch.object(auth_commands, "_sync_provider_secrets_to_supabase") as sync_mock,
        ):
            get_access_token()

        stored = load_session()
        assert stored is not None
        assert stored.provider_token == "ghp_original"
        assert stored.provider_refresh_token is None
        sync_mock.assert_called_once()
