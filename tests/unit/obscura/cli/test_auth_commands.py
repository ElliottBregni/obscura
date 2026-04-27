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
    """Disable .env auto-loading, clear the shell-env snapshot, turn the
    OS keyring off, and stub the cloud vault -- tests manage env vars
    explicitly and must not leak any of: repo ``.env``, dev's Keychain,
    dev's shell env, or a real Supabase round-trip.
    """
    from obscura.auth import secrets as _secrets
    from obscura.auth import supabase_secrets as _vault

    monkeypatch.setattr(_secrets, "_load_dotenv_once", lambda: None)
    monkeypatch.setattr(_secrets, "_dotenv_loaded", True)
    monkeypatch.setattr(_secrets, "_shell_env_snapshot", {})
    monkeypatch.setattr(_secrets, "keyring_available", lambda: False)
    monkeypatch.setattr(_vault, "get_client", lambda: None)
    _vault.reset()


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
        self,
        isolated_credentials: Path,
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
        self,
        isolated_credentials: Path,
    ) -> None:
        save_session(_sample_session())
        mode = isolated_credentials.stat().st_mode & 0o777
        assert mode == 0o600


class TestGetAccessToken:
    def test_returns_current_token_when_fresh(
        self,
        isolated_credentials: Path,
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
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("SUPABASE_URL", raising=False)
        monkeypatch.delenv("SUPABASE_ANON_KEY", raising=False)
        assert SupabaseCliConfig.from_env() is None

    def test_from_env_strips_trailing_slash(
        self,
        monkeypatch: pytest.MonkeyPatch,
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
        self,
        isolated_credentials: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _ = isolated_credentials
        monkeypatch.delenv("SUPABASE_URL", raising=False)
        monkeypatch.delenv("SUPABASE_ANON_KEY", raising=False)

        with patch.object(auth_commands, "_run_oauth_flow") as oauth_flow:
            assert ensure_github_oauth_session(open_browser=False) is None
            oauth_flow.assert_not_called()

    def test_reuses_existing_valid_session(
        self,
        isolated_credentials: Path,
        monkeypatch: pytest.MonkeyPatch,
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
        self,
        isolated_credentials: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _ = isolated_credentials
        monkeypatch.setenv("SUPABASE_URL", "https://proj.supabase.co")
        monkeypatch.setenv("SUPABASE_ANON_KEY", "anon-key")

        fresh = _sample_session(email="new@example.com", user_id="new-user")
        with patch.object(
            auth_commands, "_run_oauth_flow", return_value=fresh
        ) as oauth_flow:
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
        self,
        isolated_credentials: Path,
    ) -> None:
        _ = isolated_credentials
        assert get_github_token() is None

    def test_returns_provider_token_when_set(
        self,
        isolated_credentials: Path,
    ) -> None:
        _ = isolated_credentials
        save_session(_sample_session(provider_token="ghp_fake123"))
        assert get_github_token() == "ghp_fake123"

    def test_returns_none_when_session_has_no_provider_token(
        self,
        isolated_credentials: Path,
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
            patch.object(
                auth_commands, "_sync_provider_secrets_to_supabase"
            ) as sync_mock,
        ):
            get_access_token()

        stored = load_session()
        assert stored is not None
        assert stored.provider_token == "ghp_original"
        assert stored.provider_refresh_token is None
        sync_mock.assert_called_once()


class TestSecretsExport:
    """`obscura-auth secrets export` prints shell eval-able lines."""

    def test_bash_output_prefixed_with_export(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from obscura.auth import secrets as _secrets

        def _fake_resolve(name: str, *, default: str | None = None) -> str | None:
            return {"SUPABASE_URL": "https://proj.supabase.co"}.get(name)

        monkeypatch.setattr(_secrets, "resolve", _fake_resolve)

        result = CliRunner().invoke(auth_group, ["secrets", "export"])

        assert result.exit_code == 0
        assert "export SUPABASE_URL=https://proj.supabase.co" in result.output

    def test_bash_output_shell_escapes_dangerous_values(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Secrets with spaces or quotes must be wrapped so eval can't execute them."""
        from obscura.auth import secrets as _secrets

        hostile = "a b$(rm -rf /)c'd"

        def _fake_resolve(name: str, *, default: str | None = None) -> str | None:
            return hostile if name == "ANTHROPIC_API_KEY" else None

        monkeypatch.setattr(_secrets, "resolve", _fake_resolve)

        result = CliRunner().invoke(auth_group, ["secrets", "export"])

        assert result.exit_code == 0
        # shlex.quote wraps in single quotes; inner quotes are escaped via
        # the '\'' idiom. The `rm -rf /` must NOT appear unquoted.
        assert "export ANTHROPIC_API_KEY='a b$(rm -rf /)c'\"'\"'d'" in result.output

    def test_fish_output_uses_set_gx(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from obscura.auth import secrets as _secrets

        def _fake_resolve(name: str, *, default: str | None = None) -> str | None:
            return {"ANTHROPIC_API_KEY": "sk-ant-123"}.get(name)

        monkeypatch.setattr(_secrets, "resolve", _fake_resolve)

        result = CliRunner().invoke(
            auth_group,
            ["secrets", "export", "--shell", "fish"],
        )

        assert result.exit_code == 0
        assert "set -gx ANTHROPIC_API_KEY sk-ant-123" in result.output

    def test_omits_unset_names(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from obscura.auth import secrets as _secrets

        monkeypatch.setattr(
            _secrets,
            "resolve",
            lambda _name, **_: None,
        )

        result = CliRunner().invoke(auth_group, ["secrets", "export"])

        assert result.exit_code == 0
        assert "export " not in result.output

    def test_only_filter_limits_output(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from obscura.auth import secrets as _secrets

        calls: list[str] = []

        def _fake_resolve(name: str, *, default: str | None = None) -> str | None:
            calls.append(name)
            return "value"

        monkeypatch.setattr(_secrets, "resolve", _fake_resolve)

        result = CliRunner().invoke(
            auth_group,
            ["secrets", "export", "--only", "SUPABASE_URL,ANTHROPIC_API_KEY"],
        )

        assert result.exit_code == 0
        assert calls == ["SUPABASE_URL", "ANTHROPIC_API_KEY"]
        assert "export SUPABASE_URL=" in result.output
        assert "export ANTHROPIC_API_KEY=" in result.output


class TestSecretsSetValidation:
    """`obscura-auth secrets set` surfaces SecretsValidationError as a
    clean Click error rather than a stack trace.
    """

    def test_rejects_nul_byte_value(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from obscura.auth import secrets as _secrets

        monkeypatch.setattr(_secrets, "keyring_available", lambda: True)

        def _boom(name: str, value: str) -> bool:
            raise _secrets.SecretsValidationError(
                f"Refusing to store {name}: value contains NUL bytes.",
            )

        monkeypatch.setattr(_secrets, "store", _boom)

        result = CliRunner().invoke(
            auth_group,
            ["secrets", "set", "SUPABASE_ANON_KEY", "--value", "has\x00null"],
        )

        assert result.exit_code != 0
        assert "NUL bytes" in result.output

    def test_oversized_value_surfaces_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from obscura.auth import secrets as _secrets

        monkeypatch.setattr(_secrets, "keyring_available", lambda: True)

        def _boom(name: str, value: str) -> bool:
            raise _secrets.SecretsValidationError(
                f"Refusing to store {name}: value is 99999 bytes, "
                "exceeds the 65536-byte limit.",
            )

        monkeypatch.setattr(_secrets, "store", _boom)

        result = CliRunner().invoke(
            auth_group,
            ["secrets", "set", "SUPABASE_ANON_KEY", "--value", "x" * 10],
        )

        assert result.exit_code != 0
        assert "exceeds" in result.output


class TestSecretsStrictEnv:
    """`obscura-auth secrets strict-env` reports flag state + audit entries."""

    def test_reports_off_state_and_hint(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("OBSCURA_TOOL_ENV_STRICT", raising=False)
        monkeypatch.setenv(
            "OBSCURA_SECRETS_AUDIT_LOG",
            str(tmp_path / "audit.jsonl"),
        )

        result = CliRunner().invoke(auth_group, ["secrets", "strict-env"])

        assert result.exit_code == 0
        assert "Strict mode: off" in result.output
        assert "export OBSCURA_TOOL_ENV_STRICT=1" in result.output
        assert "No audit entries yet." in result.output

    def test_reports_on_state(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("OBSCURA_TOOL_ENV_STRICT", "1")
        monkeypatch.setenv(
            "OBSCURA_SECRETS_AUDIT_LOG",
            str(tmp_path / "audit.jsonl"),
        )

        result = CliRunner().invoke(auth_group, ["secrets", "strict-env"])

        assert result.exit_code == 0
        assert "Strict mode: ON" in result.output
        # When already on, we don't nag about how to enable.
        assert "Enable with" not in result.output

    def test_tails_existing_audit_entries(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        log = tmp_path / "audit.jsonl"
        log.write_text(
            '{"ts":"2026-04-24T10:00:00Z","event":"strict_strip","stripped":["ANTHROPIC_API_KEY"],"count":1}\n'
            '{"ts":"2026-04-24T10:01:00Z","event":"strict_strip","stripped":["GITHUB_TOKEN","NOTION_TOKEN"],"count":2}\n',
        )
        monkeypatch.setenv("OBSCURA_SECRETS_AUDIT_LOG", str(log))

        result = CliRunner().invoke(auth_group, ["secrets", "strict-env"])

        assert result.exit_code == 0
        assert "Recent entries (2 of 2)" in result.output
        assert "ANTHROPIC_API_KEY" in result.output
        assert "GITHUB_TOKEN, NOTION_TOKEN" in result.output

    def test_tail_option_limits_output(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        log = tmp_path / "audit.jsonl"
        log.write_text(
            "\n".join(
                f'{{"ts":"2026-04-24T10:0{i}:00Z","event":"strict_strip",'
                f'"stripped":["X"],"count":1}}'
                for i in range(10)
            )
            + "\n",
        )
        monkeypatch.setenv("OBSCURA_SECRETS_AUDIT_LOG", str(log))

        result = CliRunner().invoke(
            auth_group,
            ["secrets", "strict-env", "--tail", "3"],
        )

        assert result.exit_code == 0
        assert "Recent entries (3 of 10)" in result.output

    def test_clear_removes_audit_log(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        log = tmp_path / "audit.jsonl"
        log.write_text('{"ts":"2026-04-24T10:00:00Z","event":"strict_strip"}\n')
        monkeypatch.setenv("OBSCURA_SECRETS_AUDIT_LOG", str(log))

        result = CliRunner().invoke(
            auth_group,
            ["secrets", "strict-env", "--clear"],
        )

        assert result.exit_code == 0
        assert "Cleared" in result.output
        assert not log.exists()


class TestSecretsCloudCommands:
    """Smoke tests for the encrypted cloud vault CLI subcommand group.

    The vault client is stubbed -- these tests verify the CLI glue
    (confirmation prompts, ``--yes`` bypass, error surfacing), not the
    crypto itself which is covered in test_supabase_secrets.
    """

    def _stub_client(self) -> Any:
        class _Stub:
            def __init__(self) -> None:
                self.names_list: list[tuple[str, bool]] = []
                self.pushed: list[tuple[str, str, bool]] = []
                self.deleted: list[str] = []
                self.passphrase_set_to: str | None = None
                self.passphrase_cleared = False
                self._has_passphrase = False
                self._has_risky = False

            def names(self) -> list[tuple[str, bool]]:
                return self.names_list

            def push(self, name: str, value: str, *, risk: bool = False) -> None:
                self.pushed.append((name, value, risk))

            def delete(self, name: str) -> bool:
                self.deleted.append(name)
                return True

            def get(self, name: str) -> str | None:
                return None

            def snapshot(self) -> dict[str, str]:
                return {}

            def set_passphrase(self, passphrase: str) -> None:
                self.passphrase_set_to = passphrase
                self._has_passphrase = True

            def clear_passphrase(self) -> None:
                self.passphrase_cleared = True
                self._has_passphrase = False

            def has_passphrase_key(self) -> bool:
                return self._has_passphrase

            def has_risky_entries(self) -> bool:
                return self._has_risky

        return _Stub()

    def test_status_lists_names_with_risk_marker(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from obscura.auth import supabase_secrets as _vault

        stub = self._stub_client()
        stub.names_list = [("ANTHROPIC_API_KEY", False), ("GH_TOKEN", True)]
        monkeypatch.setattr(_vault, "get_client", lambda: stub)

        result = CliRunner().invoke(auth_group, ["secrets", "cloud", "status"])

        assert result.exit_code == 0
        assert "ANTHROPIC_API_KEY" in result.output
        assert "GH_TOKEN" in result.output
        assert "[risk]" in result.output

    def test_status_without_supabase_configured(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from obscura.auth import supabase_secrets as _vault

        monkeypatch.setattr(_vault, "get_client", lambda: None)

        result = CliRunner().invoke(auth_group, ["secrets", "cloud", "status"])

        assert result.exit_code != 0
        assert "Supabase is not configured" in result.output

    def test_push_blocks_never_push_names(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from obscura.auth import supabase_secrets as _vault

        stub = self._stub_client()

        def _push(name: str, value: str, *, risk: bool = False) -> None:
            raise _vault.VaultPushBlocked(f"Refusing to push {name}")

        stub.push = _push  # type: ignore[method-assign]
        monkeypatch.setattr(_vault, "get_client", lambda: stub)

        result = CliRunner().invoke(
            auth_group,
            [
                "secrets",
                "cloud",
                "push",
                "SUPABASE_URL",
                "--value",
                "anything",
                "--yes",
            ],
        )

        assert result.exit_code != 0
        assert "Refusing to push" in result.output

    def test_push_requires_confirmation_without_yes(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from obscura.auth import supabase_secrets as _vault

        stub = self._stub_client()
        monkeypatch.setattr(_vault, "get_client", lambda: stub)

        result = CliRunner().invoke(
            auth_group,
            [
                "secrets",
                "cloud",
                "push",
                "ANTHROPIC_API_KEY",
                "--value",
                "sk-ant-secret",
            ],
            input="n\n",
        )

        assert result.exit_code == 0
        assert "Aborted" in result.output
        assert stub.pushed == []

    def test_push_with_yes_flag_skips_prompt(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from obscura.auth import supabase_secrets as _vault

        stub = self._stub_client()
        monkeypatch.setattr(_vault, "get_client", lambda: stub)

        result = CliRunner().invoke(
            auth_group,
            [
                "secrets",
                "cloud",
                "push",
                "ANTHROPIC_API_KEY",
                "--value",
                "sk-ant-secret",
                "--yes",
            ],
        )

        assert result.exit_code == 0
        assert stub.pushed == [("ANTHROPIC_API_KEY", "sk-ant-secret", False)]

    def test_push_risk_prompts_for_passphrase(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """--risk without cached passphrase → interactive prompt."""
        from obscura.auth import supabase_secrets as _vault

        stub = self._stub_client()  # has_passphrase_key starts False
        monkeypatch.setattr(_vault, "get_client", lambda: stub)

        # stdin supplies the passphrase twice (confirm) then an optional
        # empty line for the Y/N (we pass --yes so no Y/N prompt fires).
        result = CliRunner().invoke(
            auth_group,
            [
                "secrets",
                "cloud",
                "push",
                "GH_TOKEN",
                "--value",
                "ghp-real",
                "--risk",
                "--yes",
            ],
            input="mypass\nmypass\n",
        )

        assert result.exit_code == 0
        assert stub.passphrase_set_to == "mypass"
        assert stub.pushed == [("GH_TOKEN", "ghp-real", True)]

    def test_delete_requires_confirmation(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from obscura.auth import supabase_secrets as _vault

        stub = self._stub_client()
        monkeypatch.setattr(_vault, "get_client", lambda: stub)

        result = CliRunner().invoke(
            auth_group,
            ["secrets", "cloud", "delete", "ANTHROPIC_API_KEY"],
            input="n\n",
        )

        assert result.exit_code == 0
        assert "Aborted" in result.output
        assert stub.deleted == []

    def test_passphrase_set_caches_key(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from obscura.auth import supabase_secrets as _vault

        stub = self._stub_client()
        monkeypatch.setattr(_vault, "get_client", lambda: stub)

        result = CliRunner().invoke(
            auth_group,
            ["secrets", "cloud", "passphrase", "set"],
            input="chosen\nchosen\n",
        )

        assert result.exit_code == 0
        assert stub.passphrase_set_to == "chosen"

    def test_passphrase_clear(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from obscura.auth import supabase_secrets as _vault

        stub = self._stub_client()
        monkeypatch.setattr(_vault, "get_client", lambda: stub)

        result = CliRunner().invoke(
            auth_group,
            ["secrets", "cloud", "passphrase", "clear"],
        )

        assert result.exit_code == 0
        assert stub.passphrase_cleared is True


class TestProfileCommands:
    """Smoke tests for `obscura-auth profile` -- the client itself is
    covered by test_profile.py; these verify the CLI glue.
    """

    def _stub_profile_client(self) -> Any:
        from obscura.auth import profile as _profile

        class _Stub:
            def __init__(self) -> None:
                self.loaded = _profile.ObscuraProfile()
                self.updates: list[dict[str, Any]] = []
                self.device_registered: str | None = None
                self.device_removed: str | None = None

            def load(self) -> Any:
                return self.loaded

            def update(self, **fields: Any) -> Any:
                self.updates.append(fields)
                self.loaded = self.loaded.model_copy(update=fields)
                return self.loaded

            def register_device(self, name: str | None = None) -> Any:
                self.device_registered = name or "auto-host"
                return _profile.DeviceInfo(
                    id="test-machine-id",
                    name=self.device_registered,
                    platform="darwin",
                    hostname="test-host",
                    first_seen="2026-04-24T00:00:00+00:00",
                    last_seen="2026-04-24T00:00:00+00:00",
                )

            def rename_device(self, new_name: str) -> Any:
                return _profile.DeviceInfo(
                    id="test-machine-id",
                    name=new_name,
                    platform="darwin",
                    hostname="test-host",
                    first_seen="2026-04-24T00:00:00+00:00",
                    last_seen="2026-04-24T00:00:00+00:00",
                )

            def remove_device(self, device_id: str) -> bool:
                self.device_removed = device_id
                return True

            def touch_device(self) -> Any:
                return None

        return _Stub()

    def test_profile_show_prints_defaults(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from obscura.auth import profile as _profile

        stub = self._stub_profile_client()
        monkeypatch.setattr(_profile, "get_client", lambda: stub)

        result = CliRunner().invoke(auth_group, ["profile", "show"])

        assert result.exit_code == 0
        assert "display_name" in result.output
        assert "none registered" in result.output.lower()

    def test_profile_set_coerces_bool(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from obscura.auth import profile as _profile

        stub = self._stub_profile_client()
        monkeypatch.setattr(_profile, "get_client", lambda: stub)

        result = CliRunner().invoke(
            auth_group,
            ["profile", "set", "undercover", "true"],
        )

        assert result.exit_code == 0
        assert stub.updates == [{"undercover": True}]

    def test_profile_set_coerces_list(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from obscura.auth import profile as _profile

        stub = self._stub_profile_client()
        monkeypatch.setattr(_profile, "get_client", lambda: stub)

        result = CliRunner().invoke(
            auth_group,
            ["profile", "set", "feature_flags", "voice,swarm"],
        )

        assert result.exit_code == 0
        assert stub.updates == [{"feature_flags": ["voice", "swarm"]}]

    def test_profile_set_rejects_unknown_field(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from obscura.auth import profile as _profile

        stub = self._stub_profile_client()
        monkeypatch.setattr(_profile, "get_client", lambda: stub)

        result = CliRunner().invoke(
            auth_group,
            ["profile", "set", "bogus", "value"],
        )

        assert result.exit_code != 0
        assert "Unknown profile field" in result.output

    def test_profile_device_register(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from obscura.auth import profile as _profile

        stub = self._stub_profile_client()
        monkeypatch.setattr(_profile, "get_client", lambda: stub)

        result = CliRunner().invoke(
            auth_group,
            ["profile", "device", "register", "--name", "laptop"],
        )

        assert result.exit_code == 0
        assert stub.device_registered == "laptop"

    def test_profile_device_remove_confirms(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from obscura.auth import profile as _profile

        stub = self._stub_profile_client()
        monkeypatch.setattr(_profile, "get_client", lambda: stub)

        result = CliRunner().invoke(
            auth_group,
            ["profile", "device", "remove", "old-machine"],
            input="n\n",
        )

        assert result.exit_code == 0
        assert "Aborted" in result.output
        assert stub.device_removed is None
