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


def test_preflight_reports_expected_keys() -> None:
    checks = session_ingest.preflight_system_session_ingest()
    assert "ready" in checks
    assert "agent_sync_script" in checks
    assert "obscura_home" in checks
    assert "cwd" in checks


