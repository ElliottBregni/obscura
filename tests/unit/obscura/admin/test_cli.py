"""CLI smoke tests for ``obscura admin delete-user``.

These exercise the click command plumbing. The deep correctness of the
deletion walk is pinned in ``test_deletion.py``; this file just makes
sure the command is registered, prompts, flags, and exit codes behave.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from obscura.cli.admin_commands import admin_group


def _invoke(args: list[str], **kwargs: object) -> object:
    runner = CliRunner()
    return runner.invoke(admin_group, args, **kwargs)  # type: ignore[arg-type]


def test_help_lists_delete_user() -> None:
    result = _invoke(["--help"])
    assert result.exit_code == 0
    assert "delete-user" in result.output


def test_delete_user_help_shows_flags() -> None:
    result = _invoke(["delete-user", "--help"])
    assert result.exit_code == 0
    for flag in ("--dry-run", "--yes", "--json"):
        assert flag in result.output


def test_confirmation_required_without_yes_or_dry_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OBSCURA_MEMORY_DIR", str(tmp_path / "m"))
    monkeypatch.setenv("OBSCURA_VECTOR_MEMORY_DIR", str(tmp_path / "v"))
    monkeypatch.setenv("OBSCURA_EVENT_STORE_PATH", str(tmp_path / "s.db"))
    monkeypatch.setenv("OBSCURA_NOTIFY_DB", str(tmp_path / "n.db"))
    monkeypatch.setenv("OBSCURA_KAIROS_DB", str(tmp_path / "k.db"))
    monkeypatch.setenv("OBSCURA_AUDIT_LOG", str(tmp_path / "audit.jsonl"))

    # Answer "no" to the prompt — command must abort with non-zero exit.
    result = _invoke(["delete-user", "alice@example.com"], input="n\n")
    assert result.exit_code == 1
    assert "Aborted" in result.output


def test_yes_flag_skips_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OBSCURA_MEMORY_DIR", str(tmp_path / "m"))
    monkeypatch.setenv("OBSCURA_VECTOR_MEMORY_DIR", str(tmp_path / "v"))
    monkeypatch.setenv("OBSCURA_EVENT_STORE_PATH", str(tmp_path / "s.db"))
    monkeypatch.setenv("OBSCURA_NOTIFY_DB", str(tmp_path / "n.db"))
    monkeypatch.setenv("OBSCURA_KAIROS_DB", str(tmp_path / "k.db"))
    monkeypatch.setenv("OBSCURA_AUDIT_LOG", str(tmp_path / "audit.jsonl"))

    result = _invoke(["delete-user", "alice@example.com", "--yes"])
    assert result.exit_code == 0
    assert "Deleted data for user_id='alice@example.com'" in result.output


def test_dry_run_skips_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OBSCURA_MEMORY_DIR", str(tmp_path / "m"))
    monkeypatch.setenv("OBSCURA_VECTOR_MEMORY_DIR", str(tmp_path / "v"))
    monkeypatch.setenv("OBSCURA_EVENT_STORE_PATH", str(tmp_path / "s.db"))
    monkeypatch.setenv("OBSCURA_NOTIFY_DB", str(tmp_path / "n.db"))
    monkeypatch.setenv("OBSCURA_KAIROS_DB", str(tmp_path / "k.db"))
    monkeypatch.setenv("OBSCURA_AUDIT_LOG", str(tmp_path / "audit.jsonl"))

    result = _invoke(["delete-user", "alice@example.com", "--dry-run"])
    assert result.exit_code == 0
    assert "Would delete" in result.output


def test_json_output_is_parseable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OBSCURA_MEMORY_DIR", str(tmp_path / "m"))
    monkeypatch.setenv("OBSCURA_VECTOR_MEMORY_DIR", str(tmp_path / "v"))
    monkeypatch.setenv("OBSCURA_EVENT_STORE_PATH", str(tmp_path / "s.db"))
    monkeypatch.setenv("OBSCURA_NOTIFY_DB", str(tmp_path / "n.db"))
    monkeypatch.setenv("OBSCURA_KAIROS_DB", str(tmp_path / "k.db"))
    monkeypatch.setenv("OBSCURA_AUDIT_LOG", str(tmp_path / "audit.jsonl"))

    result = _invoke(["delete-user", "alice@example.com", "--yes", "--json"])
    assert result.exit_code == 0
    # stderr (structlog audit log) may have been mixed into output; extract
    # the JSON block by finding the first '{' onward.
    brace = result.output.find("{")  # type: ignore[attr-defined]
    payload = json.loads(result.output[brace:])  # type: ignore[index]
    assert payload["user_id"] == "alice@example.com"
    assert payload["ok"] is True
    assert "per_store" in payload
