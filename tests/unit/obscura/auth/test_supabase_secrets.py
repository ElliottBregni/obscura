"""Tests for obscura.auth.supabase_secrets -- two-vault cloud secrets.

Covers:

* Crypto primitives roundtrip
* Regular vault (email-derived key): read / push / delete
* Risky vault (passphrase-derived key): set_passphrase unlock, push, read
* Never-push guard
* names() reports risky flag per entry
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx
from cryptography.fernet import Fernet

from obscura.auth import supabase_secrets as vault_module


@pytest.fixture(autouse=True)
def _reset_singleton() -> Any:
    vault_module.reset()
    yield
    vault_module.reset()


@pytest.fixture
def _patch_keyring(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """In-memory keyring substitute for the passphrase-key caching tests."""
    import sys
    import types

    store: dict[str, str] = {}

    class _PasswordDeleteError(Exception):
        pass

    class _KeyringError(Exception):
        pass

    errors_module = types.ModuleType("keyring.errors")
    errors_module.PasswordDeleteError = _PasswordDeleteError  # type: ignore[attr-defined]
    errors_module.KeyringError = _KeyringError  # type: ignore[attr-defined]

    fake_keyring = types.ModuleType("keyring")
    fake_keyring.errors = errors_module  # type: ignore[attr-defined]

    def _get(service: str, name: str) -> str | None:
        return store.get(f"{service}:{name}")

    def _set(service: str, name: str, value: str) -> None:
        store[f"{service}:{name}"] = value

    def _del(service: str, name: str) -> None:
        key = f"{service}:{name}"
        if key not in store:
            raise _PasswordDeleteError(name)
        del store[key]

    fake_keyring.get_password = _get  # type: ignore[attr-defined]
    fake_keyring.set_password = _set  # type: ignore[attr-defined]
    fake_keyring.delete_password = _del  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "keyring", fake_keyring)
    monkeypatch.setitem(sys.modules, "keyring.errors", errors_module)

    return store


# ---------------------------------------------------------------------------
# Crypto primitives
# ---------------------------------------------------------------------------


class TestCrypto:
    def test_derive_key_is_deterministic(self) -> None:
        salt = vault_module.generate_salt()
        k1 = vault_module.derive_key("correct-horse-battery", salt)
        k2 = vault_module.derive_key("correct-horse-battery", salt)
        assert k1 == k2

    def test_derive_key_differs_across_salts(self) -> None:
        s1 = vault_module.generate_salt()
        s2 = vault_module.generate_salt()
        assert s1 != s2
        assert vault_module.derive_key("p", s1) != vault_module.derive_key("p", s2)

    def test_derive_key_differs_across_material(self) -> None:
        salt = vault_module.generate_salt()
        assert vault_module.derive_key("a", salt) != vault_module.derive_key("b", salt)

    def test_fernet_roundtrip(self) -> None:
        salt = vault_module.generate_salt()
        key = vault_module.derive_key("pass", salt)
        token = Fernet(key).encrypt(b"value").decode("ascii")
        assert Fernet(key).decrypt(token.encode("ascii")) == b"value"

    def test_salt_encoding_roundtrip(self) -> None:
        salt = vault_module.generate_salt()
        assert vault_module.decode_salt(vault_module.encode_salt(salt)) == salt


# ---------------------------------------------------------------------------
# Client: regular vault
# ---------------------------------------------------------------------------


class TestRegularVault:
    def _make_client(self) -> vault_module.SupabaseVaultClient:
        return vault_module.SupabaseVaultClient(
            api_url="https://sb.test",
            anon_key="anon-key",
            get_access_token=lambda: "session-tok",
        )

    @respx.mock
    def test_get_decrypts_email_encrypted_entry(
        self,
        _patch_keyring: dict[str, str],
    ) -> None:
        salt = vault_module.generate_salt()
        key = vault_module.derive_key("me@example.com", salt)
        token = Fernet(key).encrypt(b"sk-ant-real").decode("ascii")

        respx.get("https://sb.test/auth/v1/user").mock(
            return_value=httpx.Response(
                200,
                json={
                    "email": "me@example.com",
                    "user_metadata": {
                        "obscura_vault_salt": vault_module.encode_salt(salt),
                        "obscura_vault": {"ANTHROPIC_API_KEY": token},
                    },
                },
            ),
        )

        client = self._make_client()
        assert client.get("ANTHROPIC_API_KEY") == "sk-ant-real"

    @respx.mock
    def test_push_encrypts_with_email_key(
        self,
        _patch_keyring: dict[str, str],
    ) -> None:
        salt = vault_module.generate_salt()

        respx.get("https://sb.test/auth/v1/user").mock(
            return_value=httpx.Response(
                200,
                json={
                    "email": "me@example.com",
                    "user_metadata": {
                        "obscura_vault_salt": vault_module.encode_salt(salt),
                    },
                },
            ),
        )
        put = respx.put("https://sb.test/auth/v1/user").mock(
            return_value=httpx.Response(200, json={}),
        )

        self._make_client().push("ANTHROPIC_API_KEY", "sk-real")

        assert put.call_count == 1
        body = put.calls[0].request.read()
        assert b"sk-real" not in body
        assert b"ANTHROPIC_API_KEY" in body
        assert b"obscura_vault" in body

    @respx.mock
    def test_push_rejects_never_push_names(
        self,
        _patch_keyring: dict[str, str],
    ) -> None:
        respx.get("https://sb.test/auth/v1/user").mock(
            return_value=httpx.Response(200, json={"user_metadata": {}}),
        )
        client = self._make_client()
        with pytest.raises(vault_module.VaultPushBlocked):
            client.push("SUPABASE_SERVICE_ROLE_KEY", "anything")

    @respx.mock
    def test_delete_removes_entry(
        self,
        _patch_keyring: dict[str, str],
    ) -> None:
        respx.get("https://sb.test/auth/v1/user").mock(
            return_value=httpx.Response(
                200,
                json={
                    "email": "me@example.com",
                    "user_metadata": {
                        "obscura_vault": {"KEEP": "ct1", "REMOVE": "ct2"},
                    },
                },
            ),
        )
        put = respx.put("https://sb.test/auth/v1/user").mock(
            return_value=httpx.Response(200, json={}),
        )

        client = self._make_client()
        assert client.delete("REMOVE") is True
        assert put.call_count == 1
        body = put.calls[0].request.read()
        assert b"KEEP" in body
        assert b"REMOVE" not in body

    @respx.mock
    def test_names_reports_regular_and_risky(
        self,
        _patch_keyring: dict[str, str],
    ) -> None:
        respx.get("https://sb.test/auth/v1/user").mock(
            return_value=httpx.Response(
                200,
                json={
                    "email": "me@example.com",
                    "user_metadata": {
                        "obscura_vault": {"REG_A": "ct", "REG_B": "ct"},
                        "obscura_vault_risk": {"RISK_X": "ct"},
                    },
                },
            ),
        )
        names = self._make_client().names()
        assert ("REG_A", False) in names
        assert ("REG_B", False) in names
        assert ("RISK_X", True) in names


# ---------------------------------------------------------------------------
# Client: risky vault (passphrase-protected)
# ---------------------------------------------------------------------------


class TestRiskyVault:
    def _make_client(self) -> vault_module.SupabaseVaultClient:
        return vault_module.SupabaseVaultClient(
            api_url="https://sb.test",
            anon_key="anon-key",
            get_access_token=lambda: "session-tok",
        )

    @respx.mock
    def test_push_with_risk_requires_cached_passphrase_key(
        self,
        _patch_keyring: dict[str, str],
    ) -> None:
        respx.get("https://sb.test/auth/v1/user").mock(
            return_value=httpx.Response(
                200,
                json={"email": "me@example.com", "user_metadata": {}},
            ),
        )
        client = self._make_client()
        with pytest.raises(vault_module.PassphraseRequired):
            client.push("GH_TOKEN", "ghp-real", risk=True)

    @respx.mock
    def test_set_passphrase_then_push_works(
        self,
        _patch_keyring: dict[str, str],
    ) -> None:
        # Initial: no salt, no vault. set_passphrase generates the risky salt.
        respx.get("https://sb.test/auth/v1/user").mock(
            return_value=httpx.Response(
                200,
                json={"email": "me@example.com", "user_metadata": {}},
            ),
        )
        respx.put("https://sb.test/auth/v1/user").mock(
            return_value=httpx.Response(200, json={}),
        )

        client = self._make_client()
        client.set_passphrase("my-passphrase")

        assert client.has_passphrase_key() is True

        # The passphrase key should now be in our fake keyring.
        assert vault_module.load_passphrase_key() is not None, (
            "passphrase key should be cached in the OS keyring slot"
        )

    @respx.mock
    def test_push_risk_moves_entry_out_of_regular_vault(
        self,
        _patch_keyring: dict[str, str],
    ) -> None:
        """push --risk deletes the name from the regular vault if present."""
        risky_salt = vault_module.generate_salt()
        regular_salt = vault_module.generate_salt()

        respx.get("https://sb.test/auth/v1/user").mock(
            return_value=httpx.Response(
                200,
                json={
                    "email": "me@example.com",
                    "user_metadata": {
                        "obscura_vault": {"GH_TOKEN": "old-regular-ct"},
                        "obscura_vault_risk": {},
                        "obscura_vault_salt": vault_module.encode_salt(regular_salt),
                        "obscura_vault_risk_salt": vault_module.encode_salt(risky_salt),
                    },
                },
            ),
        )
        put = respx.put("https://sb.test/auth/v1/user").mock(
            return_value=httpx.Response(200, json={}),
        )

        # Pre-populate the passphrase key in our fake keyring.
        vault_module.store_passphrase_key(
            vault_module.derive_key("pass", risky_salt),
        )

        client = self._make_client()
        client.push("GH_TOKEN", "ghp-real", risk=True)

        assert put.call_count == 1
        body = put.calls[0].request.read()
        assert b"obscura_vault_risk" in body
        # Old regular entry should be gone.
        import json as _json

        parsed = _json.loads(body)
        data = parsed["data"]
        assert "GH_TOKEN" not in data.get("obscura_vault", {})
        assert "GH_TOKEN" in data.get("obscura_vault_risk", {})

    @respx.mock
    def test_get_decrypts_risky_entry_when_key_cached(
        self,
        _patch_keyring: dict[str, str],
    ) -> None:
        risky_salt = vault_module.generate_salt()
        risky_key = vault_module.derive_key("pass", risky_salt)
        ct = Fernet(risky_key).encrypt(b"risky-value").decode("ascii")

        respx.get("https://sb.test/auth/v1/user").mock(
            return_value=httpx.Response(
                200,
                json={
                    "email": "me@example.com",
                    "user_metadata": {
                        "obscura_vault_risk": {"GH_TOKEN": ct},
                        "obscura_vault_risk_salt": vault_module.encode_salt(risky_salt),
                    },
                },
            ),
        )

        vault_module.store_passphrase_key(risky_key)

        client = self._make_client()
        assert client.get("GH_TOKEN") == "risky-value"

    @respx.mock
    def test_get_returns_none_for_risky_entry_without_cached_key(
        self,
        _patch_keyring: dict[str, str],
    ) -> None:
        """Resolver path: silent None, never raises."""
        respx.get("https://sb.test/auth/v1/user").mock(
            return_value=httpx.Response(
                200,
                json={
                    "email": "me@example.com",
                    "user_metadata": {
                        "obscura_vault_risk": {"GH_TOKEN": "some-ct"},
                    },
                },
            ),
        )

        client = self._make_client()
        assert client.get("GH_TOKEN") is None

    @respx.mock
    def test_has_risky_entries(
        self,
        _patch_keyring: dict[str, str],
    ) -> None:
        respx.get("https://sb.test/auth/v1/user").mock(
            return_value=httpx.Response(
                200,
                json={
                    "email": "me@example.com",
                    "user_metadata": {
                        "obscura_vault_risk": {"X": "ct"},
                    },
                },
            ),
        )
        assert self._make_client().has_risky_entries() is True

    def test_clear_passphrase_removes_cached_key(
        self,
        _patch_keyring: dict[str, str],
    ) -> None:
        vault_module.store_passphrase_key(b"some-key-bytes-32bytes-xxxxxxxxxx")
        assert vault_module.load_passphrase_key() is not None

        vault_module._clear_passphrase_key()
        assert vault_module.load_passphrase_key() is None


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------


class TestSingleton:
    def test_returns_none_when_supabase_not_configured(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from obscura.auth import secrets as _secrets

        for var in ("SUPABASE_URL", "SUPABASE_ANON_KEY"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setattr(_secrets, "_shell_env_snapshot", {})
        monkeypatch.setattr(_secrets, "_dotenv_loaded", True)
        monkeypatch.setattr(_secrets, "keyring_available", lambda: False)

        assert vault_module.get_client() is None

    def test_caches_instance(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from obscura.auth import secrets as _secrets

        monkeypatch.setattr(
            _secrets,
            "_shell_env_snapshot",
            {"SUPABASE_URL": "https://sb.test", "SUPABASE_ANON_KEY": "anon"},
        )
        monkeypatch.setattr(_secrets, "_dotenv_loaded", True)
        monkeypatch.setattr(_secrets, "keyring_available", lambda: False)

        c1 = vault_module.get_client()
        c2 = vault_module.get_client()
        assert c1 is not None
        assert c1 is c2
