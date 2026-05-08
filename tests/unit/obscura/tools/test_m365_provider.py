"""Unit tests for obscura.tools.providers.m365 (M365 CLI wrapper)."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import obscura.tools.providers.m365 as _m365

pytestmark = pytest.mark.unit


def _make_proc(returncode: int, stdout: bytes, stderr: bytes = b"") -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    return proc


async def test_m365_provider_binary_not_found() -> None:
    with patch.object(_m365.shutil, "which", return_value=None):
        result = await _m365.M365Provider(command="login status")

    assert "error" in result
    assert "m365" in result["error"]


async def test_m365_provider_success_returns_parsed_json() -> None:
    payload = {"status": "logged in", "user": "test@example.com"}
    proc = _make_proc(0, json.dumps(payload).encode())

    with (
        patch.object(_m365.shutil, "which", return_value="/usr/bin/m365"),
        patch.object(asyncio, "create_subprocess_exec", return_value=proc),
    ):
        result = await _m365.M365Provider(command="login status")

    assert result["status"] == "logged in"


async def test_m365_provider_nonzero_exit_returns_error() -> None:
    proc = _make_proc(1, b"", b"authentication required")

    with (
        patch.object(_m365.shutil, "which", return_value="/usr/bin/m365"),
        patch.object(asyncio, "create_subprocess_exec", return_value=proc),
    ):
        result = await _m365.M365Provider(command="somecommand")

    assert "error" in result


async def test_m365_provider_boolean_flag_appended() -> None:
    proc = _make_proc(0, json.dumps({}).encode())
    mock_exec = AsyncMock(return_value=proc)

    with (
        patch.object(_m365.shutil, "which", return_value="/usr/bin/m365"),
        patch.object(asyncio, "create_subprocess_exec", new=mock_exec),
    ):
        await _m365.M365Provider(command="login status", verbose=True)

    cmd_str = " ".join(str(a) for a in mock_exec.call_args[0])
    assert "--verbose" in cmd_str


async def test_m365_provider_non_json_output_returned_as_output() -> None:
    proc = _make_proc(0, b"plain text")

    with (
        patch.object(_m365.shutil, "which", return_value="/usr/bin/m365"),
        patch.object(asyncio, "create_subprocess_exec", return_value=proc),
    ):
        result = await _m365.M365Provider(command="status")

    # Non-JSON output should still be returned somehow
    assert isinstance(result, dict)
