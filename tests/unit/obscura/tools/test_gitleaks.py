"""Unit tests for obscura.tools.providers.gitleaks.

All paths go through asyncio.create_subprocess_exec. Mocked with AsyncMock.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import obscura.tools.providers.gitleaks as _gl

pytestmark = pytest.mark.unit


def _make_proc(returncode: int, stdout: bytes, stderr: bytes) -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    return proc


# ---------------------------------------------------------------------------
# Binary not found
# ---------------------------------------------------------------------------


async def test_handler_scan_repo_binary_not_found() -> None:
    with patch.object(_gl.shutil, "which", return_value=None):
        result = await _gl._handler_scan_repo(path="/some/path")

    assert "error" in result
    assert "gitleaks" in result["error"]


# ---------------------------------------------------------------------------
# Clean scan (no findings)
# ---------------------------------------------------------------------------


async def test_handler_scan_repo_clean_returns_no_findings() -> None:
    proc = _make_proc(0, b"[]", b"")

    with (
        patch.object(_gl.shutil, "which", return_value="/usr/local/bin/gitleaks"),
        patch.object(asyncio, "create_subprocess_exec", return_value=proc),
    ):
        result = await _gl._handler_scan_repo(path="/repo")

    assert result["clean"] is True
    assert result["count"] == 0
    assert result["findings"] == []


# ---------------------------------------------------------------------------
# Leaks found (exit code 1)
# ---------------------------------------------------------------------------


async def test_handler_scan_repo_leaks_found_exit_1() -> None:
    findings_json = json.dumps(
        [{"Description": "AWS Key", "File": "config.py", "LineNumber": 42}]
    ).encode()
    proc = _make_proc(1, findings_json, b"")

    with (
        patch.object(_gl.shutil, "which", return_value="/usr/local/bin/gitleaks"),
        patch.object(asyncio, "create_subprocess_exec", return_value=proc),
    ):
        result = await _gl._handler_scan_repo(path="/repo")

    assert result["clean"] is False
    assert result["count"] == 1
    assert result["findings"][0]["Description"] == "AWS Key"


# ---------------------------------------------------------------------------
# Unexpected exit code (real error)
# ---------------------------------------------------------------------------


async def test_handler_scan_repo_unexpected_exit_returns_error() -> None:
    proc = _make_proc(2, b"", b"permission denied")

    with (
        patch.object(_gl.shutil, "which", return_value="/usr/local/bin/gitleaks"),
        patch.object(asyncio, "create_subprocess_exec", return_value=proc),
    ):
        result = await _gl._handler_scan_repo(path="/repo")

    assert "error" in result
    assert "permission denied" in result["error"]


# ---------------------------------------------------------------------------
# Malformed JSON output
# ---------------------------------------------------------------------------


async def test_handler_scan_repo_malformed_json_returns_raw_output() -> None:
    proc = _make_proc(0, b"not json at all", b"")

    with (
        patch.object(_gl.shutil, "which", return_value="/usr/local/bin/gitleaks"),
        patch.object(asyncio, "create_subprocess_exec", return_value=proc),
    ):
        result = await _gl._handler_scan_repo(path="/repo")

    assert "output" in result


# ---------------------------------------------------------------------------
# Empty stdout (no output)
# ---------------------------------------------------------------------------


async def test_handler_scan_repo_empty_stdout_returns_clean() -> None:
    proc = _make_proc(0, b"", b"")

    with (
        patch.object(_gl.shutil, "which", return_value="/usr/local/bin/gitleaks"),
        patch.object(asyncio, "create_subprocess_exec", return_value=proc),
    ):
        result = await _gl._handler_scan_repo(path="/repo")

    assert result["clean"] is True
    assert result["count"] == 0


# ---------------------------------------------------------------------------
# Exception from subprocess
# ---------------------------------------------------------------------------


async def test_handler_scan_repo_subprocess_exception_returns_error() -> None:
    with (
        patch.object(_gl.shutil, "which", return_value="/usr/local/bin/gitleaks"),
        patch.object(
            asyncio,
            "create_subprocess_exec",
            side_effect=OSError("failed to spawn"),
        ),
    ):
        result = await _gl._handler_scan_repo(path="/repo")

    assert "error" in result
