"""Tests for vault-zone write guards in file write tools.

vault/user/ and vault/shared/ are read-only from the agent's perspective.
vault/agent/ is the only zone agents may write to.
Paths outside the vault are unaffected.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from obscura.tools.system import (
    append_text_file,
    edit_text_file,
    remove_path,
    write_text_file,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def run(coro: Any) -> Any:  # noqa: ANN401
    return asyncio.run(coro)


def parse(result: str) -> dict[str, Any]:
    return json.loads(result)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def fake_vault(tmp_path: Path) -> Path:
    """Create a minimal vault tree and point resolve_obscura_home at it."""
    vault = tmp_path / "vault"
    (vault / "user").mkdir(parents=True)
    (vault / "shared").mkdir(parents=True)
    (vault / "agent").mkdir(parents=True)
    return tmp_path  # this is the fake obscura home


# ---------------------------------------------------------------------------
# _is_vault_write_allowed unit tests
# ---------------------------------------------------------------------------


def test_vault_write_allowed_outside_vault(tmp_path: Path) -> None:
    """Paths outside the vault are always allowed."""
    from obscura.tools.system import _is_vault_write_allowed

    with patch("obscura.core.paths.resolve_obscura_home", return_value=tmp_path / ".obscura"):
        result = _is_vault_write_allowed(tmp_path / "some_file.txt")
    assert result is True


def test_vault_write_blocked_user_zone(fake_vault: Path) -> None:
    from obscura.tools.system import _is_vault_write_allowed

    target = fake_vault / "vault" / "user" / "profile.md"
    with patch("obscura.core.paths.resolve_obscura_home", return_value=fake_vault):
        result = _is_vault_write_allowed(target)
    assert result is False


def test_vault_write_blocked_shared_zone(fake_vault: Path) -> None:
    from obscura.tools.system import _is_vault_write_allowed

    target = fake_vault / "vault" / "shared" / "notes.md"
    with patch("obscura.core.paths.resolve_obscura_home", return_value=fake_vault):
        result = _is_vault_write_allowed(target)
    assert result is False


def test_vault_write_allowed_agent_zone(fake_vault: Path) -> None:
    from obscura.tools.system import _is_vault_write_allowed

    target = fake_vault / "vault" / "agent" / "scratch.md"
    with patch("obscura.core.paths.resolve_obscura_home", return_value=fake_vault):
        result = _is_vault_write_allowed(target)
    assert result is True


# ---------------------------------------------------------------------------
# write_text_file integration tests
# ---------------------------------------------------------------------------


def test_write_text_file_blocked_in_vault_user(fake_vault: Path) -> None:
    target = str(fake_vault / "vault" / "user" / "profile.md")
    with patch("obscura.core.paths.resolve_obscura_home", return_value=fake_vault):
        result = parse(run(write_text_file(target, "content")))
    assert result["ok"] is False
    assert result["error"] == "vault_zone_readonly"
    assert "vault/agent" in result["detail"]


def test_write_text_file_allowed_in_vault_agent(fake_vault: Path) -> None:
    target = str(fake_vault / "vault" / "agent" / "scratch.txt")
    with patch("obscura.core.paths.resolve_obscura_home", return_value=fake_vault):
        result = parse(run(write_text_file(target, "hello")))
    assert result["ok"] is True
    assert Path(target).read_text() == "hello"


# ---------------------------------------------------------------------------
# append_text_file integration tests
# ---------------------------------------------------------------------------


def test_append_text_file_blocked_in_vault_user(fake_vault: Path) -> None:
    target = str(fake_vault / "vault" / "user" / "profile.md")
    with patch("obscura.core.paths.resolve_obscura_home", return_value=fake_vault):
        result = parse(run(append_text_file(target, "content")))
    assert result["ok"] is False
    assert result["error"] == "vault_zone_readonly"


def test_append_text_file_allowed_in_vault_agent(fake_vault: Path) -> None:
    target = str(fake_vault / "vault" / "agent" / "log.txt")
    with patch("obscura.core.paths.resolve_obscura_home", return_value=fake_vault):
        result = parse(run(append_text_file(target, "line\n")))
    assert result["ok"] is True


# ---------------------------------------------------------------------------
# edit_text_file integration tests
# ---------------------------------------------------------------------------


def test_edit_text_file_blocked_in_vault_user(fake_vault: Path) -> None:
    # Create the file first (bypassing the tool) so the guard is reached.
    target_path = fake_vault / "vault" / "user" / "profile.md"
    target_path.write_text("original")
    target = str(target_path)
    with patch("obscura.core.paths.resolve_obscura_home", return_value=fake_vault):
        result = parse(run(edit_text_file(target, "original", "modified")))
    assert result["ok"] is False
    assert result["error"] == "vault_zone_readonly"
    # File must be unmodified.
    assert target_path.read_text() == "original"


# ---------------------------------------------------------------------------
# remove_path integration tests
# ---------------------------------------------------------------------------


def test_remove_path_blocked_in_vault_user(fake_vault: Path) -> None:
    target_path = fake_vault / "vault" / "user" / "profile.md"
    target_path.write_text("keep me")
    target = str(target_path)
    with patch("obscura.core.paths.resolve_obscura_home", return_value=fake_vault):
        result = parse(run(remove_path(target)))
    assert result["ok"] is False
    assert result["error"] == "vault_zone_readonly"
    assert target_path.exists(), "file must not have been deleted"


def test_remove_path_allowed_in_vault_agent(fake_vault: Path) -> None:
    target_path = fake_vault / "vault" / "agent" / "tmp.txt"
    target_path.write_text("bye")
    target = str(target_path)
    with patch("obscura.core.paths.resolve_obscura_home", return_value=fake_vault):
        result = parse(run(remove_path(target)))
    assert result["ok"] is True
    assert not target_path.exists()
