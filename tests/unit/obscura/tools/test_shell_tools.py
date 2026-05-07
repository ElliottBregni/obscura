"""Unit tests for shell / process execution tools."""
# pyright: reportPrivateUsage=false

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from obscura.tools.system._shell import Shell

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_proc(
    stdout: bytes = b"",
    stderr: bytes = b"",
    returncode: int = 0,
) -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.wait = AsyncMock()
    proc.kill = MagicMock()
    return proc


# ---------------------------------------------------------------------------
# run_python3
# ---------------------------------------------------------------------------


async def test_run_python3_success() -> None:
    proc = _fake_proc(stdout=b"hello\n", returncode=0)
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        result = json.loads(await Shell.run_python3(code='print("hello")'))

    assert result["ok"] is True
    assert "hello" in result["stdout"]
    assert result["exit_code"] == 0


async def test_run_python3_nonzero_exit() -> None:
    proc = _fake_proc(stderr=b"SyntaxError: invalid syntax\n", returncode=1)
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        result = json.loads(await Shell.run_python3(code="???"))

    assert result["ok"] is False
    assert result["exit_code"] == 1
    assert "SyntaxError" in result["stderr"]


async def test_run_python3_timeout() -> None:
    async def _slow_communicate() -> tuple[bytes, bytes]:
        await asyncio.sleep(9999)
        return b"", b""

    proc = MagicMock()
    proc.returncode = -1
    proc.communicate = _slow_communicate
    proc.kill = MagicMock()
    proc.wait = AsyncMock()

    with patch("asyncio.create_subprocess_exec", return_value=proc):
        result = json.loads(await Shell.run_python3(code="x", timeout_seconds=0.001))

    assert result["ok"] is False
    assert "timeout" in result.get("error", "")


# ---------------------------------------------------------------------------
# run_command
# ---------------------------------------------------------------------------


async def test_run_command_success() -> None:
    proc = _fake_proc(stdout=b"output\n", returncode=0)
    with (
        patch("asyncio.create_subprocess_exec", return_value=proc),
        patch("shutil.which", return_value="/usr/bin/echo"),
        patch.dict("os.environ", {"OBSCURA_UNSAFE_FULL_ACCESS": "1"}),
    ):
        result = json.loads(await Shell.run_command(command="echo", args=["hi"]))

    assert result["ok"] is True
    assert result["exit_code"] == 0


async def test_run_command_nonzero_exit() -> None:
    proc = _fake_proc(stderr=b"not found\n", returncode=127)
    with (
        patch("asyncio.create_subprocess_exec", return_value=proc),
        patch("shutil.which", return_value="/usr/bin/false"),
        patch.dict("os.environ", {"OBSCURA_UNSAFE_FULL_ACCESS": "1"}),
    ):
        result = json.loads(await Shell.run_command(command="false"))

    assert result["exit_code"] == 127


async def test_run_command_empty_command_returns_error() -> None:
    with patch.dict("os.environ", {"OBSCURA_UNSAFE_FULL_ACCESS": "1"}):
        result = json.loads(await Shell.run_command(command="   "))
    assert result["ok"] is False


async def test_run_command_denied_in_default_policy() -> None:
    # 'rm' is in DEFAULT_DENIED_COMMANDS
    result = json.loads(await Shell.run_command(command="rm"))
    assert result["ok"] is False


# ---------------------------------------------------------------------------
# run_shell
# ---------------------------------------------------------------------------


async def test_run_shell_success() -> None:
    proc = _fake_proc(stdout=b"done\n", returncode=0)
    with (
        patch("asyncio.create_subprocess_exec", return_value=proc),
        patch("shutil.which", return_value="/bin/zsh"),
        patch.dict("os.environ", {"OBSCURA_UNSAFE_FULL_ACCESS": "1"}),
    ):
        result = json.loads(await Shell.run_shell(script="echo done"))

    assert result["ok"] is True
    assert "done" in result["stdout"]


async def test_run_shell_empty_script_returns_error() -> None:
    result = json.loads(await Shell.run_shell(script=""))
    assert result["ok"] is False


async def test_run_shell_uses_script_over_command() -> None:
    """run_shell prefers the 'script' kwarg over 'command' (legacy alias)."""
    proc = _fake_proc(stdout=b"from_script\n", returncode=0)
    captured: list[str] = []

    async def fake_exec(*args: object, **_kwargs: object) -> MagicMock:
        captured.extend(str(a) for a in args)
        return proc

    with (
        patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
        patch("shutil.which", return_value="/bin/zsh"),
        patch.dict("os.environ", {"OBSCURA_UNSAFE_FULL_ACCESS": "1"}),
    ):
        await Shell.run_shell(script="echo from_script", command="echo from_command")

    # The actual script passed to zsh should be the 'script' value
    joined = " ".join(captured)
    assert "from_script" in joined
