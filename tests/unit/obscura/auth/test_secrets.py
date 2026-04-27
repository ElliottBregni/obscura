"""Tests for obscura.auth.secrets -- layered secret resolver.

The resolver walks **shell env > OS keyring > dotenv-loaded env > default**.
Keyring access and the shell-env snapshot are both patched so these tests
never touch the real macOS Keychain and never depend on what the developer
happens to have exported.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from obscura.auth import secrets as secrets_module


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_audit_log(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> Any:
    """Redirect the audit log to a tmp file so strict-mode tests don't
    litter the developer's ``~/.obscura/logs/``.
    """
    monkeypatch.setenv(
        "OBSCURA_SECRETS_AUDIT_LOG",
        str(tmp_path / "audit.jsonl"),
    )
    return tmp_path / "audit.jsonl"


@pytest.fixture(autouse=True)
def _no_vault(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable the Supabase cloud vault tier in resolver tests.

    Each test that cares about the vault installs its own stub via
    ``_fake_vault``; the default here prevents accidental live network
    calls or SupabaseCliConfig resolution noise.
    """
    from obscura.auth import supabase_secrets as _vault

    monkeypatch.setattr(_vault, "get_client", lambda: None)
    _vault.reset()


@pytest.fixture
def _fake_vault(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Install an in-memory vault snapshot for tests that need one."""
    from obscura.auth import supabase_secrets as _vault

    store: dict[str, str] = {}

    class _FakeVault:
        def get(self, name: str) -> str | None:
            return store.get(name)

        def snapshot(self) -> dict[str, str]:
            return dict(store)

        def is_locked(self) -> bool:
            return False

    monkeypatch.setattr(_vault, "get_client", lambda: _FakeVault())
    return store


@pytest.fixture
def _no_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulate a host with no keyring backend (Docker / headless Linux)."""
    monkeypatch.setattr(secrets_module, "keyring_available", lambda: False)


@pytest.fixture
def _fake_keyring(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Install an in-memory keyring so tests can assert round-trips.

    ``secrets.delete()`` imports ``keyring`` *and* ``keyring.errors`` to
    catch ``PasswordDeleteError`` -- we have to stub both submodules or
    the second import falls through to ``except Exception`` and masks
    the success path.
    """
    import sys
    import types

    store: dict[str, str] = {}

    class _KeyringError(Exception):
        pass

    class _PasswordDeleteError(_KeyringError):
        pass

    errors_module = types.ModuleType("keyring.errors")
    errors_module.PasswordDeleteError = _PasswordDeleteError  # type: ignore[attr-defined]
    errors_module.KeyringError = _KeyringError  # type: ignore[attr-defined]

    fake_keyring = types.ModuleType("keyring")
    fake_keyring.errors = errors_module  # type: ignore[attr-defined]

    def _get_password(service: str, name: str) -> str | None:
        return store.get(f"{service}:{name}")

    def _set_password(service: str, name: str, value: str) -> None:
        store[f"{service}:{name}"] = value

    def _delete_password(service: str, name: str) -> None:
        key = f"{service}:{name}"
        if key not in store:
            raise _PasswordDeleteError(name)
        del store[key]

    fake_keyring.get_password = _get_password  # type: ignore[attr-defined]
    fake_keyring.set_password = _set_password  # type: ignore[attr-defined]
    fake_keyring.delete_password = _delete_password  # type: ignore[attr-defined]

    monkeypatch.setattr(secrets_module, "keyring_available", lambda: True)
    monkeypatch.setitem(sys.modules, "keyring", fake_keyring)
    monkeypatch.setitem(sys.modules, "keyring.errors", errors_module)

    return store


def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset both os.environ and the shell snapshot so tests start clean.

    Without this, tests inherit whatever the developer has exported locally
    (or whatever a prior test left behind), leading to flaky behaviour that
    depends on CI vs. laptop environment.
    """
    for name in secrets_module.KNOWN_SECRET_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(secrets_module, "_dotenv_loaded", True)
    monkeypatch.setattr(secrets_module, "_shell_env_snapshot", {})


def _set_shell_env(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
) -> None:
    """Simulate ``export NAME=value`` in the shell before obscura started.

    Writes into *both* the snapshot (for the resolver's tier-1 check) and
    ``os.environ`` (for anyone reading live env).
    """
    snapshot = dict(secrets_module._shell_env_snapshot)
    snapshot[name] = value
    monkeypatch.setattr(secrets_module, "_shell_env_snapshot", snapshot)
    monkeypatch.setenv(name, value)


def _set_dotenv_env(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
) -> None:
    """Simulate a ``.env`` file having set NAME after obscura started.

    The snapshot is untouched; only ``os.environ`` gets the value, exactly
    like ``python-dotenv`` does at load time.
    """
    monkeypatch.setenv(name, value)


# ---------------------------------------------------------------------------
# Resolve precedence
# ---------------------------------------------------------------------------


class TestResolvePrecedence:
    def test_shell_env_wins_over_keyring(
        self,
        monkeypatch: pytest.MonkeyPatch,
        _fake_keyring: dict[str, str],
    ) -> None:
        """Shell exports are sacred -- the operator set them deliberately."""
        _clear_env(monkeypatch)
        _fake_keyring["obscura-cli:SUPABASE_URL"] = "https://keyring.example"
        _set_shell_env(monkeypatch, "SUPABASE_URL", "https://shell.example")

        assert secrets_module.resolve("SUPABASE_URL") == "https://shell.example"

    def test_keyring_wins_over_dotenv(
        self,
        monkeypatch: pytest.MonkeyPatch,
        _fake_keyring: dict[str, str],
    ) -> None:
        """Unrelated ``.env`` files cannot shadow a key in the user's keychain."""
        _clear_env(monkeypatch)
        _fake_keyring["obscura-cli:SUPABASE_URL"] = "https://keyring.example"
        _set_dotenv_env(monkeypatch, "SUPABASE_URL", "https://dotenv.example")

        assert secrets_module.resolve("SUPABASE_URL") == "https://keyring.example"

    def test_dotenv_used_when_keyring_empty(
        self,
        monkeypatch: pytest.MonkeyPatch,
        _no_keyring: None,
    ) -> None:
        _clear_env(monkeypatch)
        _set_dotenv_env(monkeypatch, "SUPABASE_URL", "https://dotenv.example")

        assert secrets_module.resolve("SUPABASE_URL") == "https://dotenv.example"

    def test_keyring_fallback_when_env_missing(
        self,
        monkeypatch: pytest.MonkeyPatch,
        _fake_keyring: dict[str, str],
    ) -> None:
        _clear_env(monkeypatch)
        _fake_keyring["obscura-cli:SUPABASE_JWT_SECRET"] = "s3cret"

        assert secrets_module.resolve("SUPABASE_JWT_SECRET") == "s3cret"

    def test_default_when_nothing_configured(
        self,
        monkeypatch: pytest.MonkeyPatch,
        _no_keyring: None,
    ) -> None:
        _clear_env(monkeypatch)

        assert secrets_module.resolve("SUPABASE_URL", default="fallback") == "fallback"
        assert secrets_module.resolve("SUPABASE_URL") is None

    def test_whitespace_only_shell_env_falls_through(
        self,
        monkeypatch: pytest.MonkeyPatch,
        _fake_keyring: dict[str, str],
    ) -> None:
        _clear_env(monkeypatch)
        _set_shell_env(monkeypatch, "SUPABASE_URL", "   ")
        _fake_keyring["obscura-cli:SUPABASE_URL"] = "https://keyring.example"

        assert secrets_module.resolve("SUPABASE_URL") == "https://keyring.example"

    def test_no_keyring_backend_still_returns_dotenv(
        self,
        monkeypatch: pytest.MonkeyPatch,
        _no_keyring: None,
    ) -> None:
        _clear_env(monkeypatch)
        _set_dotenv_env(monkeypatch, "SUPABASE_URL", "https://dotenv.example")

        assert secrets_module.resolve("SUPABASE_URL") == "https://dotenv.example"


# ---------------------------------------------------------------------------
# store / delete
# ---------------------------------------------------------------------------


class TestSupabaseVaultTier:
    """The cloud vault sits between keyring and dotenv and is opt-in --
    all failure modes (not configured, locked, network error) must fall
    through silently to the next tier.
    """

    def test_resolves_from_vault_when_keyring_empty(
        self,
        monkeypatch: pytest.MonkeyPatch,
        _no_keyring: None,
        _fake_vault: dict[str, str],
    ) -> None:
        _clear_env(monkeypatch)
        _fake_vault["ANTHROPIC_API_KEY"] = "from-cloud"

        assert secrets_module.resolve("ANTHROPIC_API_KEY") == "from-cloud"

    def test_keyring_wins_over_vault(
        self,
        monkeypatch: pytest.MonkeyPatch,
        _fake_keyring: dict[str, str],
        _fake_vault: dict[str, str],
    ) -> None:
        _clear_env(monkeypatch)
        _fake_keyring["obscura-cli:ANTHROPIC_API_KEY"] = "from-keyring"
        _fake_vault["ANTHROPIC_API_KEY"] = "from-cloud"

        assert secrets_module.resolve("ANTHROPIC_API_KEY") == "from-keyring"

    def test_vault_wins_over_dotenv(
        self,
        monkeypatch: pytest.MonkeyPatch,
        _no_keyring: None,
        _fake_vault: dict[str, str],
    ) -> None:
        _clear_env(monkeypatch)
        _fake_vault["ANTHROPIC_API_KEY"] = "from-cloud"
        _set_dotenv_env(monkeypatch, "ANTHROPIC_API_KEY", "from-dotenv")

        assert secrets_module.resolve("ANTHROPIC_API_KEY") == "from-cloud"

    def test_shell_still_wins_over_vault(
        self,
        monkeypatch: pytest.MonkeyPatch,
        _no_keyring: None,
        _fake_vault: dict[str, str],
    ) -> None:
        _clear_env(monkeypatch)
        _set_shell_env(monkeypatch, "ANTHROPIC_API_KEY", "from-shell")
        _fake_vault["ANTHROPIC_API_KEY"] = "from-cloud"

        assert secrets_module.resolve("ANTHROPIC_API_KEY") == "from-shell"

    def test_sources_reports_supabase_label(
        self,
        monkeypatch: pytest.MonkeyPatch,
        _no_keyring: None,
        _fake_vault: dict[str, str],
    ) -> None:
        _clear_env(monkeypatch)
        _fake_vault["ANTHROPIC_API_KEY"] = "from-cloud"

        result = secrets_module.sources(["ANTHROPIC_API_KEY", "GITHUB_TOKEN"])

        assert result["ANTHROPIC_API_KEY"] == "supabase"
        assert result["GITHUB_TOKEN"] == "missing"

    def test_materialize_pulls_vault_when_keyring_empty(
        self,
        monkeypatch: pytest.MonkeyPatch,
        _no_keyring: None,
        _fake_vault: dict[str, str],
    ) -> None:
        _clear_env(monkeypatch)
        monkeypatch.setattr(secrets_module, "_materialized_names", set())
        _fake_vault["ANTHROPIC_API_KEY"] = "from-cloud"

        copied = secrets_module.materialize_to_environ()

        assert "ANTHROPIC_API_KEY" in copied
        assert os.environ["ANTHROPIC_API_KEY"] == "from-cloud"

    def test_bootstrap_names_bypass_vault_tier(
        self,
        monkeypatch: pytest.MonkeyPatch,
        _no_keyring: None,
    ) -> None:
        """SUPABASE_URL and SUPABASE_ANON_KEY must NEVER consult the
        vault tier -- doing so would recurse through get_client ->
        from_env -> resolve -> vault -> infinite loop. Even if a broken
        vault stub tries to return something, the resolver should
        still get its bootstrap config from env/default.
        """
        from obscura.auth import supabase_secrets as _vault

        # A stub that would raise if anyone calls it. We assert that it
        # DOESN'T get called for SUPABASE_URL.
        called_for: list[str] = []

        class _TrapClient:
            def get(self, name: str) -> str | None:
                called_for.append(name)
                return "should-never-return-this"

            def snapshot(self) -> dict[str, str]:
                return {}

            def is_locked(self) -> bool:
                return False

        monkeypatch.setattr(_vault, "get_client", lambda: _TrapClient())

        _clear_env(monkeypatch)
        _set_dotenv_env(monkeypatch, "SUPABASE_URL", "https://real.example")

        assert secrets_module.resolve("SUPABASE_URL") == "https://real.example"
        assert "SUPABASE_URL" not in called_for


class TestStoreAndDelete:
    def test_store_round_trips_through_keyring(
        self,
        monkeypatch: pytest.MonkeyPatch,
        _fake_keyring: dict[str, str],
    ) -> None:
        _clear_env(monkeypatch)

        assert secrets_module.store("SUPABASE_ANON_KEY", "anon-123") is True
        assert _fake_keyring["obscura-cli:SUPABASE_ANON_KEY"] == "anon-123"
        assert secrets_module.resolve("SUPABASE_ANON_KEY") == "anon-123"

    def test_store_returns_false_when_no_backend(
        self,
        _no_keyring: None,
    ) -> None:
        assert secrets_module.store("SUPABASE_ANON_KEY", "anon-123") is False

    def test_store_rejects_nul_bytes(
        self,
        _fake_keyring: dict[str, str],
    ) -> None:
        with pytest.raises(secrets_module.SecretsValidationError, match="NUL"):
            secrets_module.store("SUPABASE_ANON_KEY", "has\x00null")

    def test_store_rejects_oversized_value(
        self,
        _fake_keyring: dict[str, str],
    ) -> None:
        giant = "x" * (64 * 1024 + 1)
        with pytest.raises(secrets_module.SecretsValidationError, match="bytes"):
            secrets_module.store("SUPABASE_ANON_KEY", giant)

    def test_store_validates_before_checking_backend(
        self,
        _no_keyring: None,
    ) -> None:
        """Validation fires regardless of whether keyring is present -- a bad
        value is a caller bug that should surface even on hosts without a
        keyring backend (otherwise you'd silently "succeed" at nothing).
        """
        with pytest.raises(secrets_module.SecretsValidationError):
            secrets_module.store("SUPABASE_ANON_KEY", "has\x00null")

    def test_delete_removes_from_keyring(
        self,
        monkeypatch: pytest.MonkeyPatch,
        _fake_keyring: dict[str, str],
    ) -> None:
        _clear_env(monkeypatch)
        _fake_keyring["obscura-cli:SUPABASE_ANON_KEY"] = "anon-123"

        assert secrets_module.delete("SUPABASE_ANON_KEY") is True
        assert "obscura-cli:SUPABASE_ANON_KEY" not in _fake_keyring

    def test_delete_returns_false_when_absent(
        self,
        monkeypatch: pytest.MonkeyPatch,
        _fake_keyring: dict[str, str],
    ) -> None:
        _clear_env(monkeypatch)

        assert secrets_module.delete("SUPABASE_ANON_KEY") is False


# ---------------------------------------------------------------------------
# sources()
# ---------------------------------------------------------------------------


class TestSources:
    def test_reports_shell_keyring_dotenv_and_missing(
        self,
        monkeypatch: pytest.MonkeyPatch,
        _fake_keyring: dict[str, str],
    ) -> None:
        _clear_env(monkeypatch)
        _set_shell_env(monkeypatch, "SUPABASE_URL", "https://shell.example")
        _fake_keyring["obscura-cli:SUPABASE_ANON_KEY"] = "anon-123"
        _set_dotenv_env(monkeypatch, "SUPABASE_JWT_SECRET", "jwt-from-env-file")

        result = secrets_module.sources()

        assert result["SUPABASE_URL"] == "shell"
        assert result["SUPABASE_ANON_KEY"] == "keyring"
        assert result["SUPABASE_JWT_SECRET"] == "dotenv"
        assert result["SUPABASE_SERVICE_ROLE_KEY"] == "missing"

    def test_keyring_source_reported_even_when_dotenv_also_has_value(
        self,
        monkeypatch: pytest.MonkeyPatch,
        _fake_keyring: dict[str, str],
    ) -> None:
        """When both keyring and dotenv have a value, the listing shows
        the one the resolver actually uses -- keyring.
        """
        _clear_env(monkeypatch)
        _fake_keyring["obscura-cli:SUPABASE_URL"] = "https://keyring.example"
        _set_dotenv_env(monkeypatch, "SUPABASE_URL", "https://dotenv.example")

        assert secrets_module.sources()["SUPABASE_URL"] == "keyring"


# ---------------------------------------------------------------------------
# mask()
# ---------------------------------------------------------------------------


class TestMask:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (None, "(unset)"),
            ("", "(unset)"),
            ("short", "***"),
            ("1234567890abcdef", "***cdef"),
        ],
    )
    def test_mask_redacts_appropriately(
        self,
        value: str | None,
        expected: str,
    ) -> None:
        assert secrets_module.mask(value) == expected


# ---------------------------------------------------------------------------
# Name catalogs
# ---------------------------------------------------------------------------


def test_known_names_include_required_supabase_config() -> None:
    required = {
        "SUPABASE_URL",
        "SUPABASE_ANON_KEY",
        "SUPABASE_JWT_SECRET",
        "SUPABASE_SERVICE_ROLE_KEY",
    }
    assert required.issubset(set(secrets_module.SUPABASE_SECRET_NAMES))


def test_known_names_include_backend_keys() -> None:
    """The catalog must cover every LLM backend env var so `/secrets set`
    accepts them without --force.
    """
    required = {
        "GITHUB_TOKEN",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "MOONSHOT_API_KEY",
    }
    assert required.issubset(set(secrets_module.KNOWN_SECRET_NAMES))


def test_sensitive_names_exclude_public_url() -> None:
    assert "SUPABASE_URL" not in secrets_module.SENSITIVE_SECRET_NAMES
    assert "SUPABASE_SERVICE_ROLE_KEY" in secrets_module.SENSITIVE_SECRET_NAMES
    assert "ANTHROPIC_API_KEY" in secrets_module.SENSITIVE_SECRET_NAMES
    assert "GITHUB_TOKEN" in secrets_module.SENSITIVE_SECRET_NAMES


# ---------------------------------------------------------------------------
# materialize_to_environ()
# ---------------------------------------------------------------------------


class TestMaterializeToEnviron:
    def test_copies_keyring_values_when_env_missing(
        self,
        monkeypatch: pytest.MonkeyPatch,
        _fake_keyring: dict[str, str],
    ) -> None:
        _clear_env(monkeypatch)
        _fake_keyring["obscura-cli:ANTHROPIC_API_KEY"] = "kr-anthropic"
        _fake_keyring["obscura-cli:NOTION_TOKEN"] = "kr-notion"

        copied = secrets_module.materialize_to_environ()

        assert "ANTHROPIC_API_KEY" in copied
        assert "NOTION_TOKEN" in copied
        assert os.environ.get("ANTHROPIC_API_KEY") == "kr-anthropic"
        assert os.environ.get("NOTION_TOKEN") == "kr-notion"

    def test_overrides_dotenv_value_with_keyring(
        self,
        monkeypatch: pytest.MonkeyPatch,
        _fake_keyring: dict[str, str],
    ) -> None:
        """This is the load-bearing change: a repo-local .env cannot mask
        a keyring value for downstream plugins that read os.environ.
        """
        _clear_env(monkeypatch)
        _set_dotenv_env(monkeypatch, "ANTHROPIC_API_KEY", "from-dotenv")
        _fake_keyring["obscura-cli:ANTHROPIC_API_KEY"] = "from-keyring"

        copied = secrets_module.materialize_to_environ()

        assert "ANTHROPIC_API_KEY" in copied
        assert os.environ["ANTHROPIC_API_KEY"] == "from-keyring"

    def test_never_overrides_shell_env(
        self,
        monkeypatch: pytest.MonkeyPatch,
        _fake_keyring: dict[str, str],
    ) -> None:
        """Shell env (captured in the snapshot) is sacred -- keyring must
        not clobber what the operator deliberately exported.
        """
        _clear_env(monkeypatch)
        _set_shell_env(monkeypatch, "ANTHROPIC_API_KEY", "from-shell")
        _fake_keyring["obscura-cli:ANTHROPIC_API_KEY"] = "from-keyring"

        copied = secrets_module.materialize_to_environ()

        assert "ANTHROPIC_API_KEY" not in copied
        assert os.environ["ANTHROPIC_API_KEY"] == "from-shell"

    def test_returns_empty_list_without_keyring(
        self,
        monkeypatch: pytest.MonkeyPatch,
        _no_keyring: None,
    ) -> None:
        _clear_env(monkeypatch)

        assert secrets_module.materialize_to_environ() == []

    def test_respects_explicit_name_list(
        self,
        monkeypatch: pytest.MonkeyPatch,
        _fake_keyring: dict[str, str],
    ) -> None:
        _clear_env(monkeypatch)
        _fake_keyring["obscura-cli:ANTHROPIC_API_KEY"] = "kr-anthropic"
        _fake_keyring["obscura-cli:NOTION_TOKEN"] = "kr-notion"

        copied = secrets_module.materialize_to_environ(["ANTHROPIC_API_KEY"])

        assert copied == ["ANTHROPIC_API_KEY"]
        assert "NOTION_TOKEN" not in os.environ


class TestSafeSubprocessEnv:
    """The helper is a thin filter by design -- tests lock in the promises
    that callers rely on: default is pass-through, strict mode strips
    known + materialised names, extras always flow through.
    """

    def _base(self) -> dict[str, str]:
        return {
            "PATH": "/usr/bin",
            "ANTHROPIC_API_KEY": "sk-ant-secret",
            "CUSTOM_APP_KEY": "custom-secret",
            "UNRELATED": "ok",
        }

    def test_default_is_passthrough(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No env flag => identical to the base dict (plus extras)."""
        monkeypatch.delenv("OBSCURA_TOOL_ENV_STRICT", raising=False)
        monkeypatch.setattr(secrets_module, "_materialized_names", set())

        result = secrets_module.safe_subprocess_env(base=self._base())

        assert result == self._base()

    def test_extras_merge_on_top_of_base(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("OBSCURA_TOOL_ENV_STRICT", raising=False)
        monkeypatch.setattr(secrets_module, "_materialized_names", set())

        result = secrets_module.safe_subprocess_env(
            {"EXTRA": "yes", "PATH": "/override"},
            base=self._base(),
        )

        assert result["EXTRA"] == "yes"
        assert result["PATH"] == "/override"
        assert result["ANTHROPIC_API_KEY"] == "sk-ant-secret"

    def test_strict_mode_strips_known_secret_names(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("OBSCURA_TOOL_ENV_STRICT", "1")
        monkeypatch.setattr(secrets_module, "_materialized_names", set())

        result = secrets_module.safe_subprocess_env(base=self._base())

        assert "ANTHROPIC_API_KEY" not in result
        assert result["PATH"] == "/usr/bin"
        assert result["UNRELATED"] == "ok"
        # Unknown custom names are NOT stripped by name alone -- strict
        # mode is conservative, only strips what we're certain is a secret.
        assert result["CUSTOM_APP_KEY"] == "custom-secret"

    def test_strict_mode_also_strips_materialized_names(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A --force'd secret (outside the catalog) still gets stripped
        when we've recorded that we materialised it.
        """
        monkeypatch.setenv("OBSCURA_TOOL_ENV_STRICT", "1")
        monkeypatch.setattr(
            secrets_module,
            "_materialized_names",
            {"CUSTOM_APP_KEY"},
        )

        result = secrets_module.safe_subprocess_env(base=self._base())

        assert "CUSTOM_APP_KEY" not in result
        assert result["UNRELATED"] == "ok"

    def test_strict_mode_extras_bypass_stripping(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A tool that genuinely needs a secret can pass it via ``extras``
        and strict mode must not drop it -- this is the escape hatch.
        """
        monkeypatch.setenv("OBSCURA_TOOL_ENV_STRICT", "1")
        monkeypatch.setattr(secrets_module, "_materialized_names", set())

        result = secrets_module.safe_subprocess_env(
            {"ANTHROPIC_API_KEY": "explicit-opt-in"},
            base=self._base(),
        )

        assert result["ANTHROPIC_API_KEY"] == "explicit-opt-in"

    @pytest.mark.parametrize(
        "flag_value",
        ["1", "true", "True", "TRUE", "yes", "on"],
    )
    def test_strict_mode_flag_accepts_truthy_strings(
        self,
        monkeypatch: pytest.MonkeyPatch,
        flag_value: str,
    ) -> None:
        monkeypatch.setenv("OBSCURA_TOOL_ENV_STRICT", flag_value)
        monkeypatch.setattr(secrets_module, "_materialized_names", set())

        result = secrets_module.safe_subprocess_env(base=self._base())

        assert "ANTHROPIC_API_KEY" not in result

    @pytest.mark.parametrize(
        "flag_value",
        ["0", "false", "no", "off", ""],
    )
    def test_strict_mode_flag_rejects_falsy_strings(
        self,
        monkeypatch: pytest.MonkeyPatch,
        flag_value: str,
    ) -> None:
        monkeypatch.setenv("OBSCURA_TOOL_ENV_STRICT", flag_value)
        monkeypatch.setattr(secrets_module, "_materialized_names", set())

        result = secrets_module.safe_subprocess_env(base=self._base())

        assert result["ANTHROPIC_API_KEY"] == "sk-ant-secret"

    def test_materialize_records_names_for_stripping(
        self,
        monkeypatch: pytest.MonkeyPatch,
        _fake_keyring: dict[str, str],
    ) -> None:
        """End-to-end: materialise a keyring value, then assert the
        helper strips it when strict mode is on.
        """
        _clear_env(monkeypatch)
        monkeypatch.setattr(secrets_module, "_materialized_names", set())
        _fake_keyring["obscura-cli:ANTHROPIC_API_KEY"] = "from-keyring"

        secrets_module.materialize_to_environ()

        assert "ANTHROPIC_API_KEY" in secrets_module._materialized_names

        monkeypatch.setenv("OBSCURA_TOOL_ENV_STRICT", "1")
        result = secrets_module.safe_subprocess_env(
            base={"ANTHROPIC_API_KEY": "from-keyring", "PATH": "/usr/bin"},
        )
        assert "ANTHROPIC_API_KEY" not in result


class TestStrictModeAuditLog:
    """When strict mode strips something, it MUST land in the audit log --
    that's the whole point of the opt-in: the operator sees what happened.
    """

    def _base(self) -> dict[str, str]:
        return {
            "PATH": "/usr/bin",
            "ANTHROPIC_API_KEY": "sk-ant-secret",
            "GITHUB_TOKEN": "ghp-secret",
        }

    def test_strict_mode_writes_audit_entry(
        self,
        monkeypatch: pytest.MonkeyPatch,
        _isolate_audit_log: Any,
    ) -> None:
        import json as _json

        monkeypatch.setenv("OBSCURA_TOOL_ENV_STRICT", "1")
        monkeypatch.setattr(secrets_module, "_materialized_names", set())

        secrets_module.safe_subprocess_env(base=self._base())

        log_path = secrets_module.audit_log_path()
        assert log_path.exists()
        lines = log_path.read_text().splitlines()
        assert len(lines) == 1
        entry = _json.loads(lines[0])
        assert entry["event"] == "strict_strip"
        assert set(entry["stripped"]) == {"ANTHROPIC_API_KEY", "GITHUB_TOKEN"}
        assert entry["count"] == 2
        assert "ts" in entry

    def test_strict_mode_no_audit_when_nothing_stripped(
        self,
        monkeypatch: pytest.MonkeyPatch,
        _isolate_audit_log: Any,
    ) -> None:
        """Strict mode with an env that has no secrets to strip should NOT
        spam the audit log with empty events.
        """
        monkeypatch.setenv("OBSCURA_TOOL_ENV_STRICT", "1")
        monkeypatch.setattr(secrets_module, "_materialized_names", set())

        secrets_module.safe_subprocess_env(base={"PATH": "/usr/bin"})

        log_path = secrets_module.audit_log_path()
        assert not log_path.exists() or log_path.read_text() == ""

    def test_default_mode_never_writes_audit(
        self,
        monkeypatch: pytest.MonkeyPatch,
        _isolate_audit_log: Any,
    ) -> None:
        """Default (non-strict) mode must never touch the audit log."""
        monkeypatch.delenv("OBSCURA_TOOL_ENV_STRICT", raising=False)
        monkeypatch.setattr(secrets_module, "_materialized_names", set())

        secrets_module.safe_subprocess_env(base=self._base())

        log_path = secrets_module.audit_log_path()
        assert not log_path.exists()

    def test_audit_log_write_failure_does_not_raise(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If the log path isn't writable, ``safe_subprocess_env`` must
        still return — we can't afford to brick a subprocess spawn on a
        broken audit setup.
        """
        monkeypatch.setenv("OBSCURA_TOOL_ENV_STRICT", "1")
        monkeypatch.setattr(secrets_module, "_materialized_names", set())
        # Point the log at a path that can't be created.
        monkeypatch.setenv(
            "OBSCURA_SECRETS_AUDIT_LOG",
            "/this/path/cannot/be/created/ever.jsonl",
        )

        # Must not raise.
        result = secrets_module.safe_subprocess_env(base=self._base())
        assert "ANTHROPIC_API_KEY" not in result


class TestDotenvLoadOrder:
    """SOC2 finding D1 — ``~/.obscura/.env`` must win over ``./.env``.

    Pre-fix, :func:`_load_dotenv_once` loaded the CWD ``.env`` first with
    ``override=False``, so a checked-in repo ``.env`` could shadow secrets
    the user kept in ``~/.obscura/.env``. The CLAUDE.md docs say the
    precedence runs ``shell > ~/.obscura/.env > ./.obscura/.env > ./.env``;
    these tests pin that order against the implementation.
    """

    def test_user_home_wins_over_cwd(
        self,
        tmp_path: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cwd = tmp_path / "repo"
        cwd.mkdir()
        (cwd / ".env").write_text("ANTHROPIC_API_KEY=cwd-poisoned\n")

        home = tmp_path / "obscura-home"
        home.mkdir()
        (home / ".env").write_text("ANTHROPIC_API_KEY=user-real\n")

        monkeypatch.chdir(cwd)
        monkeypatch.setenv("OBSCURA_HOME", str(home))
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setattr(secrets_module, "_dotenv_loaded", False)

        secrets_module._load_dotenv_once()
        assert os.environ["ANTHROPIC_API_KEY"] == "user-real"

    def test_project_obscura_dir_beats_cwd(
        self,
        tmp_path: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cwd = tmp_path / "repo"
        (cwd / ".obscura").mkdir(parents=True)
        (cwd / ".env").write_text("OPENAI_API_KEY=cwd-loses\n")
        (cwd / ".obscura" / ".env").write_text("OPENAI_API_KEY=project-wins\n")

        home = tmp_path / "obscura-home"
        home.mkdir()  # no .env here, so this tier is skipped

        monkeypatch.chdir(cwd)
        monkeypatch.setenv("OBSCURA_HOME", str(home))
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.setattr(secrets_module, "_dotenv_loaded", False)

        secrets_module._load_dotenv_once()
        assert os.environ["OPENAI_API_KEY"] == "project-wins"


# Silence unused-fixture linting -- pytest fixtures are invoked by name.
_ = Any
