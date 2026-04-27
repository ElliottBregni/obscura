"""Integration tests for the keyring layer inside the provider resolvers.

The resolvers must prefer a keyring value over an env var in the
default oauth_first mode — that's the whole point of the batch. In
env_first mode, env still wins.
"""

from __future__ import annotations

import pytest

from obscura.core import auth


@pytest.fixture(autouse=True)
def _unset_all_creds(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "COPILOT_GITHUB_TOKEN",
        "ANTHROPIC_API_KEY",
        "CLAUDE_API_KEY",
        "CLAUDE_CODE_API_KEY",
        "OPENAI_API_KEY",
        "CODEX_API_KEY",
        "OBSCURA_AUTH_MODE",
        "OBSCURA_GITHUB_TOKEN_CMD",
    ):
        monkeypatch.delenv(var, raising=False)


def _patch_keyring(
    monkeypatch: pytest.MonkeyPatch,
    mapping: dict[str, str | None],
) -> None:
    def fake_secret(name: str) -> str | None:
        return mapping.get(name)

    monkeypatch.setattr(auth, "_keyring_secret", fake_secret)


# ---------------------------------------------------------------------------
# GitHub token
# ---------------------------------------------------------------------------


def test_keyring_value_wins_over_env_in_oauth_first_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_keyring(monkeypatch, {"github:token": "ghp_keyring"})
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_env")
    # Keep gh CLI out of the picture so this doesn't reach out to
    # whatever the test host has configured.
    monkeypatch.setenv("OBSCURA_GH_CLI_CMD", "/bin/false")

    assert auth._resolve_github_token(None) == "ghp_keyring"


def test_env_wins_in_env_first_mode_even_if_keyring_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_keyring(monkeypatch, {"github:token": "ghp_keyring"})
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_env")
    monkeypatch.setenv("OBSCURA_AUTH_MODE", "env_first")

    assert auth._resolve_github_token(None) == "ghp_env"


def test_explicit_beats_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_keyring(monkeypatch, {"github:token": "ghp_keyring"})
    assert auth._resolve_github_token("ghp_explicit") == "ghp_explicit"


# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------


def test_anthropic_keyring_wins_over_env(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_keyring(monkeypatch, {"anthropic:api_key": "sk-ant-keyring"})
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-env")
    assert auth._resolve_anthropic_key(None) == "sk-ant-keyring"


def test_anthropic_falls_through_when_keyring_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_keyring(monkeypatch, {})
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-env")
    assert auth._resolve_anthropic_key(None) == "sk-ant-env"


# ---------------------------------------------------------------------------
# OpenAI
# ---------------------------------------------------------------------------


def test_openai_keyring_wins_over_env(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_keyring(monkeypatch, {"openai:api_key": "sk-keyring"})
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    # Don't let codex OAuth interfere with the test.
    monkeypatch.setenv("OBSCURA_CODEX_CLI_CMD", "/bin/false")
    assert auth._resolve_openai_key(None) == "sk-keyring"


def test_openai_falls_through_when_keyring_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_keyring(monkeypatch, {})
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    monkeypatch.setenv("OBSCURA_CODEX_CLI_CMD", "/bin/false")
    assert auth._resolve_openai_key(None) == "sk-env"


# ---------------------------------------------------------------------------
# Moonshot
# ---------------------------------------------------------------------------


def test_moonshot_keyring_wins_over_env(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_keyring(monkeypatch, {"moonshot:api_key": "sk-moonshot-keyring"})
    monkeypatch.setenv("MOONSHOT_API_KEY", "sk-moonshot-env")
    assert auth._resolve_moonshot_key(None) == "sk-moonshot-keyring"


def test_moonshot_falls_through_when_keyring_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_keyring(monkeypatch, {})
    monkeypatch.setenv("MOONSHOT_API_KEY", "sk-moonshot-env")
    assert auth._resolve_moonshot_key(None) == "sk-moonshot-env"


# ---------------------------------------------------------------------------
# Defensive: underlying keyring failure must not crash the wrapper
# ---------------------------------------------------------------------------


def test_keyring_internal_exception_is_swallowed_by_wrapper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The _keyring_secret wrapper must never raise — if the keyring
    package blows up (platform quirk, stale credential, whatever), we
    want the resolver to fall through to env vars, not crash.
    """

    def boom(*_args: object, **_kwargs: object) -> str | None:
        raise RuntimeError("keyring is on fire")

    # Patch secret_store.get_secret itself (not the wrapper). The
    # wrapper's try/except is the surface we're exercising.
    from obscura.core import secret_store

    monkeypatch.setattr(secret_store, "get_secret", boom)
    assert auth._keyring_secret("anthropic:api_key") is None
