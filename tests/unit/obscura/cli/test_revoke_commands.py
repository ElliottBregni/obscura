"""CLI smoke tests for ``obscura revoke …``."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from click.testing import CliRunner

from obscura.auth import revocation as revocation_mod
from obscura.cli.revoke_commands import revoke_group


@pytest.fixture(autouse=True)
def _isolated_blocklist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    monkeypatch.setenv("OBSCURA_REVOCATIONS_DB", str(tmp_path / "r.db"))
    revocation_mod.reset_default_blocklist()
    yield
    revocation_mod.reset_default_blocklist()


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_help_lists_subcommands(runner: CliRunner) -> None:
    result = runner.invoke(revoke_group, ["--help"])  # type: ignore[arg-type]
    assert result.exit_code == 0
    for sub in ("token", "user", "list", "purge"):
        assert sub in result.output


def test_revoke_token_records_entry(runner: CliRunner) -> None:
    result = runner.invoke(
        revoke_group,
        [
            "token",
            "jti-alpha",
            "--user-id",
            "alice",
            "--reason",
            "compromised",
            "--expires-in",
            "60",
        ],
    )  # type: ignore[arg-type]
    assert result.exit_code == 0
    assert "Revoked jti=jti-alpha" in result.output
    assert revocation_mod.default_blocklist().is_revoked("jti-alpha")


def test_revoke_token_rejects_empty_jti(runner: CliRunner) -> None:
    result = runner.invoke(revoke_group, ["token", "   "])  # type: ignore[arg-type]
    assert result.exit_code != 0


def test_revoke_user_requires_jti(runner: CliRunner) -> None:
    result = runner.invoke(revoke_group, ["user", "alice"])  # type: ignore[arg-type]
    assert result.exit_code != 0


def test_revoke_user_writes_all_jtis(runner: CliRunner) -> None:
    result = runner.invoke(
        revoke_group,
        [
            "user",
            "alice",
            "--jti",
            "j1",
            "--jti",
            "j2",
            "--reason",
            "rotation",
        ],
    )  # type: ignore[arg-type]
    assert result.exit_code == 0
    assert "Revoked 2 token(s)" in result.output
    for jti in ("j1", "j2"):
        assert revocation_mod.default_blocklist().is_revoked(jti)


def test_list_for_user_shows_records(runner: CliRunner) -> None:
    import time

    bl = revocation_mod.default_blocklist()
    bl.revoke("j1", user_id="alice", expires_at=time.time() + 60)
    bl.revoke("j2", user_id="alice", expires_at=time.time() + 60)
    bl.revoke("j3", user_id="bob", expires_at=time.time() + 60)
    result = runner.invoke(revoke_group, ["list", "alice"])  # type: ignore[arg-type]
    assert result.exit_code == 0
    assert "j1" in result.output
    assert "j2" in result.output
    assert "j3" not in result.output


def test_purge_removes_expired(runner: CliRunner) -> None:
    import time

    bl = revocation_mod.default_blocklist()
    bl.revoke("stale", expires_at=time.time() - 10)
    bl.revoke("live", expires_at=time.time() + 60)
    result = runner.invoke(revoke_group, ["purge"])  # type: ignore[arg-type]
    assert result.exit_code == 0
    assert "Purged 1" in result.output
