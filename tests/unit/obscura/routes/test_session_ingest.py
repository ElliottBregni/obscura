"""Tests for session ingest helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

from obscura.auth.models import AuthenticatedUser
from obscura.routes import session_ingest


def _user() -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id="u1",
        email="u1@example.com",
        roles=("sessions:manage",),
        org_id="local",
        token_type="user",
        raw_token="",
    )


class _Completed:
    def __init__(self, returncode: int = 0, stderr: str = "") -> None:
        self.returncode = returncode
        self.stderr = stderr


def test_sync_uses_absolute_agent_sync_path_and_project_cwd() -> None:
    calls: list[dict[str, Any]] = []

    def _fake_run(cmd: list[str], **kwargs: Any) -> _Completed:
        calls.append({"cmd": cmd, **kwargs})
        return _Completed(returncode=0)

    with (
        patch("obscura.routes.session_ingest.subprocess.run", side_effect=_fake_run),
        patch("obscura.routes.session_ingest._load_index_entries", return_value=[]),
        patch(
            "obscura.routes.session_ingest._ingest_entries_for_user",
            return_value=(0, 0),
        ),
    ):
        out = session_ingest.sync_and_ingest_system_sessions(_user())

    assert out["synced"] is True
    assert calls, "subprocess.run should be called"
    called = calls[0]
    cmd = called["cmd"]
    assert cmd[0]
    assert cmd[1] == str(session_ingest._AGENT_SYNC_SCRIPT)
    assert cmd[2] == "--skip-memory"
    assert Path(called["cwd"]) == session_ingest._PROJECT_ROOT


def test_preflight_reports_expected_keys() -> None:
    checks = session_ingest.preflight_system_session_ingest()
    assert "ready" in checks
    assert "agent_sync_script" in checks
    assert "obscura_home" in checks
    assert "cwd" in checks


def test_sync_supports_copy_to_pwd_flag() -> None:
    calls: list[dict[str, Any]] = []

    def _fake_run(cmd: list[str], **kwargs: Any) -> _Completed:
        calls.append({"cmd": cmd, **kwargs})
        return _Completed(returncode=0)

    with (
        patch("obscura.routes.session_ingest.subprocess.run", side_effect=_fake_run),
        patch("obscura.routes.session_ingest._load_index_entries", return_value=[]),
        patch(
            "obscura.routes.session_ingest._ingest_entries_for_user",
            return_value=(0, 0),
        ),
        patch(
            "obscura.routes.session_ingest.copy_obscura_to_pwd",
            return_value={"copied": True},
        ) as mock_copy,
    ):
        out = session_ingest.sync_and_ingest_system_sessions(
            _user(),
            copy_to_pwd=True,
            copy_overwrite=False,
        )

    assert out["copy_to_pwd"] is True
    assert out["copy_result"] == {"copied": True}
    mock_copy.assert_called_once_with(overwrite=False)
    assert calls, "subprocess.run should be called"
