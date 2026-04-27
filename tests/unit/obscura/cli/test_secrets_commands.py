"""CLI smoke tests for ``obscura secrets …``."""

from __future__ import annotations

import sys
import types

import pytest
from click.testing import CliRunner


class _FakeKeyring:
    def __init__(self) -> None:
        self._store: dict[tuple[str, str], str] = {}

    def get_keyring(self) -> object:
        class _B:
            pass

        b = _B()
        b.__class__.__name__ = "RealKeyring"
        return b

    def get_password(self, service: str, account: str) -> str | None:
        return self._store.get((service, account))

    def set_password(self, service: str, account: str, value: str) -> None:
        self._store[(service, account)] = value

    def delete_password(self, service: str, account: str) -> None:
        self._store.pop((service, account), None)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def fake_keyring(monkeypatch: pytest.MonkeyPatch) -> _FakeKeyring:
    fake = _FakeKeyring()
    module = types.ModuleType("keyring")
    module.get_keyring = fake.get_keyring  # type: ignore[attr-defined]
    module.get_password = fake.get_password  # type: ignore[attr-defined]
    module.set_password = fake.set_password  # type: ignore[attr-defined]
    module.delete_password = fake.delete_password  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "keyring", module)
    return fake


def _cmd() -> object:
    from obscura.cli.secrets_commands import secrets_group

    return secrets_group


def test_status_lists_known_slots(runner: CliRunner) -> None:
    result = runner.invoke(_cmd(), ["status"])  # type: ignore[arg-type]
    assert result.exit_code == 0
    assert "github:token" in result.output
    assert "anthropic:api_key" in result.output


def test_set_prompts_for_value_hidden(runner: CliRunner) -> None:
    result = runner.invoke(_cmd(), ["set", "github:token"], input="ghp_secret\n")  # type: ignore[arg-type]
    assert result.exit_code == 0
    assert "Stored github:token" in result.output
    # NEVER echo the value back.
    assert "ghp_secret" not in result.output


def test_set_accepts_value_flag(runner: CliRunner) -> None:
    result = runner.invoke(_cmd(), ["set", "github:token", "--value", "ghp_x"])  # type: ignore[arg-type]
    assert result.exit_code == 0


def test_set_rejects_empty(runner: CliRunner) -> None:
    result = runner.invoke(_cmd(), ["set", "github:token", "--value", "   "])  # type: ignore[arg-type]
    assert result.exit_code != 0


def test_show_does_not_print_value(runner: CliRunner, fake_keyring: _FakeKeyring) -> None:
    fake_keyring.set_password("obscura", "github:token", "ghp_supersecret_value_here")
    result = runner.invoke(_cmd(), ["show", "github:token"])  # type: ignore[arg-type]
    assert result.exit_code == 0
    assert "stored" in result.output
    assert "ghp_supersecret_value_here" not in result.output


def test_show_missing_returns_nonzero(runner: CliRunner) -> None:
    result = runner.invoke(_cmd(), ["show", "github:token"])  # type: ignore[arg-type]
    assert result.exit_code != 0


def test_list_shows_stored_names_only(
    runner: CliRunner, fake_keyring: _FakeKeyring
) -> None:
    fake_keyring.set_password("obscura", "github:token", "ghp_x")
    result = runner.invoke(_cmd(), ["list"])  # type: ignore[arg-type]
    assert result.exit_code == 0
    assert "github:token" in result.output
    assert "ghp_x" not in result.output


def test_delete_requires_confirmation(
    runner: CliRunner, fake_keyring: _FakeKeyring
) -> None:
    fake_keyring.set_password("obscura", "github:token", "ghp_x")
    # Answer no.
    result = runner.invoke(_cmd(), ["delete", "github:token"], input="n\n")  # type: ignore[arg-type]
    assert result.exit_code != 0
    assert fake_keyring.get_password("obscura", "github:token") == "ghp_x"


def test_delete_with_yes_removes(
    runner: CliRunner, fake_keyring: _FakeKeyring
) -> None:
    fake_keyring.set_password("obscura", "github:token", "ghp_x")
    result = runner.invoke(_cmd(), ["delete", "github:token", "--yes"])  # type: ignore[arg-type]
    assert result.exit_code == 0
    assert fake_keyring.get_password("obscura", "github:token") is None
