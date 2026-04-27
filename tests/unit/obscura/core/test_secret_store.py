"""Tests for obscura.core.secret_store.

We mock the keyring module itself because real keyring writes persist
across test runs and some CI platforms have no backend.
"""

from __future__ import annotations

import sys
import types

import pytest

from obscura.core import secret_store


class _FakeKeyring:
    """Minimal stand-in for the keyring module used by the store."""

    def __init__(self, *, available: bool = True) -> None:
        self._available = available
        self._store: dict[tuple[str, str], str] = {}

    def get_keyring(self) -> object:
        class _Backend:
            pass

        backend = _Backend()
        # Flip the class name to match what is_available looks for.
        backend.__class__.__name__ = (
            "NullKeyring" if not self._available else "RealKeyring"
        )
        return backend

    def get_password(self, service: str, account: str) -> str | None:
        return self._store.get((service, account))

    def set_password(self, service: str, account: str, value: str) -> None:
        self._store[(service, account)] = value

    def delete_password(self, service: str, account: str) -> None:
        self._store.pop((service, account), None)


@pytest.fixture
def fake_keyring(monkeypatch: pytest.MonkeyPatch) -> _FakeKeyring:
    fake = _FakeKeyring()
    module = types.ModuleType("keyring")
    module.get_keyring = fake.get_keyring  # type: ignore[attr-defined]
    module.get_password = fake.get_password  # type: ignore[attr-defined]
    module.set_password = fake.set_password  # type: ignore[attr-defined]
    module.delete_password = fake.delete_password  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "keyring", module)
    return fake


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------


def test_is_available_false_when_module_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Simulate "import keyring" raising ImportError.
    monkeypatch.setitem(sys.modules, "keyring", None)
    assert secret_store.is_available() is False


def test_is_available_false_when_backend_is_null(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeKeyring(available=False)
    module = types.ModuleType("keyring")
    module.get_keyring = fake.get_keyring  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "keyring", module)
    assert secret_store.is_available() is False


def test_is_available_true_with_real_backend(fake_keyring: _FakeKeyring) -> None:
    assert secret_store.is_available() is True


# ---------------------------------------------------------------------------
# Read / write / delete
# ---------------------------------------------------------------------------


def test_set_and_get_round_trip(fake_keyring: _FakeKeyring) -> None:
    assert secret_store.set_secret("github:token", "ghp_whatever") is True
    assert secret_store.get_secret("github:token") == "ghp_whatever"


def test_get_returns_none_for_missing(fake_keyring: _FakeKeyring) -> None:
    assert secret_store.get_secret("openai:api_key") is None


def test_set_strips_whitespace(fake_keyring: _FakeKeyring) -> None:
    secret_store.set_secret("github:token", "  value  \n")
    assert secret_store.get_secret("github:token") == "value"


def test_set_rejects_empty() -> None:
    with pytest.raises(ValueError):
        secret_store.set_secret("github:token", "")
    with pytest.raises(ValueError):
        secret_store.set_secret("github:token", "   ")


def test_delete_removes_value(fake_keyring: _FakeKeyring) -> None:
    secret_store.set_secret("anthropic:api_key", "sk-ant-test")
    assert secret_store.delete_secret("anthropic:api_key") is True
    assert secret_store.get_secret("anthropic:api_key") is None


def test_delete_missing_is_quiet(fake_keyring: _FakeKeyring) -> None:
    # Should return True or False but never raise.
    result = secret_store.delete_secret("never-set")
    assert isinstance(result, bool)


def test_missing_module_degrades_cleanly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "keyring", None)
    assert secret_store.get_secret("github:token") is None
    assert secret_store.set_secret("github:token", "x") is False
    assert secret_store.delete_secret("github:token") is False


# ---------------------------------------------------------------------------
# list_stored and KNOWN_SECRETS
# ---------------------------------------------------------------------------


def test_list_stored_reports_known_slots(fake_keyring: _FakeKeyring) -> None:
    secret_store.set_secret("github:token", "ghp_x")
    secret_store.set_secret("anthropic:api_key", "sk-ant-y")
    names = set(secret_store.list_stored())
    assert "github:token" in names
    assert "anthropic:api_key" in names
    assert "openai:api_key" not in names


def test_known_secrets_covers_every_provider_resolver() -> None:
    # If a new provider resolver is added that reads from the keyring,
    # add it to KNOWN_SECRETS too. This test fails loud if someone
    # forgets, rather than leaving a resolver without a management UI.
    names = {name for name, _ in secret_store.KNOWN_SECRETS}
    for expected in (
        "github:token",
        "anthropic:api_key",
        "openai:api_key",
        "moonshot:api_key",
    ):
        assert expected in names, f"missing {expected}"
