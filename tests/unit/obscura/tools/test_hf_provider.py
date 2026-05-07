"""Unit tests for obscura.tools.providers.hf (Hugging Face Hub CLI)."""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import obscura.tools.providers.hf as _hf

pytestmark = pytest.mark.unit


def _make_proc(returncode: int, stdout: bytes, stderr: bytes = b"") -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    return proc


async def test_hf_provider_binary_not_found() -> None:
    with patch.object(_hf.shutil, "which", return_value=None):
        result = await _hf.HFProvider(command="whoami")

    assert "error" in result
    assert "hf" in result["error"]


async def test_hf_provider_whoami_success() -> None:
    payload = {"name": "testuser"}
    proc = _make_proc(0, json.dumps(payload).encode())

    with (
        patch.object(_hf.shutil, "which", return_value="/usr/bin/hf"),
        patch.object(asyncio, "create_subprocess_exec", return_value=proc),
    ):
        result = await _hf.HFProvider(_tool_name="hf.whoami")

    assert isinstance(result, dict)


async def test_hf_provider_tool_name_maps_to_subcommand() -> None:
    proc = _make_proc(0, b"{}")
    mock_exec = AsyncMock(return_value=proc)

    with (
        patch.object(_hf.shutil, "which", return_value="/usr/bin/hf"),
        patch.object(asyncio, "create_subprocess_exec", new=mock_exec),
    ):
        await _hf.HFProvider(_tool_name="hf.repo.list")

    cmd_str = " ".join(str(a) for a in mock_exec.call_args[0])
    assert "repo" in cmd_str
    assert "list" in cmd_str


async def test_hf_provider_raw_command_passed_through() -> None:
    proc = _make_proc(0, b"{}")
    mock_exec = AsyncMock(return_value=proc)

    with (
        patch.object(_hf.shutil, "which", return_value="/usr/bin/hf"),
        patch.object(asyncio, "create_subprocess_exec", new=mock_exec),
    ):
        await _hf.HFProvider(command="repo list --limit 5")

    cmd_str = " ".join(str(a) for a in mock_exec.call_args[0])
    assert "repo" in cmd_str or "list" in cmd_str


async def test_hf_provider_nonzero_exit_returns_error() -> None:
    proc = _make_proc(1, b"", b"authentication failed")

    with (
        patch.object(_hf.shutil, "which", return_value="/usr/bin/hf"),
        patch.object(asyncio, "create_subprocess_exec", return_value=proc),
    ):
        result = await _hf.HFProvider(command="status")

    assert "error" in result


async def test_hf_provider_exception_returns_error() -> None:
    with (
        patch.object(_hf.shutil, "which", return_value="/usr/bin/hf"),
        patch.object(
            asyncio,
            "create_subprocess_exec",
            side_effect=OSError("spawn failed"),
        ),
    ):
        result = await _hf.HFProvider(command="whoami")

    assert "error" in result
