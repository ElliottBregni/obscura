"""Unit tests for process/system tools:
Process.get_environment, Process.get_system_info, Process.list_processes,
Shell.which_command."""
# pyright: reportPrivateUsage=false

from __future__ import annotations

import json

import pytest

from obscura.tools.system._process import Process
from obscura.tools.system._shell import Shell

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# get_environment
# ---------------------------------------------------------------------------


async def test_get_environment_contains_injected_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OBSCURA_TEST_XYZ_VAR", "hello")

    result = json.loads(await Process.get_environment())

    assert result["ok"] is True
    assert "OBSCURA_TEST_XYZ_VAR" in result["variables"]


async def test_get_environment_prefix_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_PREFIX_ALPHA", "a")
    monkeypatch.setenv("OTHER_BETA", "b")

    result = json.loads(await Process.get_environment(prefix="MY_PREFIX_"))

    assert result["ok"] is True
    vars_returned = result["variables"]
    assert "MY_PREFIX_ALPHA" in vars_returned
    assert "OTHER_BETA" not in vars_returned


async def test_get_environment_include_values_false_returns_none_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OBSCURA_TEST_SECRET", "hidden")

    result = json.loads(
        await Process.get_environment(
            prefix="OBSCURA_TEST_SECRET", include_values=False
        )
    )

    assert result["ok"] is True
    assert result["variables"]["OBSCURA_TEST_SECRET"] is None


async def test_get_environment_include_values_true_returns_actual_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OBSCURA_TEST_VISIBLE", "exposed")

    result = json.loads(
        await Process.get_environment(
            prefix="OBSCURA_TEST_VISIBLE", include_values=True
        )
    )

    assert result["ok"] is True
    assert result["variables"]["OBSCURA_TEST_VISIBLE"] == "exposed"


async def test_get_environment_count_matches_variables_length(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OBSCURA_COUNT_A", "1")
    monkeypatch.setenv("OBSCURA_COUNT_B", "2")

    result = json.loads(await Process.get_environment(prefix="OBSCURA_COUNT_"))

    assert result["ok"] is True
    assert result["count"] == len(result["variables"])
    assert result["count"] >= 2


# ---------------------------------------------------------------------------
# get_system_info
# ---------------------------------------------------------------------------


async def test_get_system_info_returns_ok() -> None:
    result = json.loads(await Process.get_system_info())

    assert result["ok"] is True
    info = result["info"]
    assert "platform" in info
    assert "python_version" in info
    assert "cwd" in info


async def test_get_system_info_has_command_availability() -> None:
    result = json.loads(await Process.get_system_info())

    assert result["ok"] is True
    # "commands" key lists common tool paths (may be None if not installed)
    assert "commands" in result["info"]
    assert "git" in result["info"]["commands"]


# ---------------------------------------------------------------------------
# list_processes
# ---------------------------------------------------------------------------


async def test_list_processes_returns_ok() -> None:
    """ps runs on all Unix test platforms; result should be ok=True."""
    result = json.loads(await Process.list_processes())
    assert result["ok"] is True


async def test_list_processes_output_contains_pid_header() -> None:
    result = json.loads(await Process.list_processes())
    assert result["ok"] is True
    # ps -ax output always starts with a PID column header
    assert "PID" in result.get("stdout", "")


# ---------------------------------------------------------------------------
# Shell.which_command
# ---------------------------------------------------------------------------


async def test_which_command_finds_ls() -> None:
    """`ls` exists on every POSIX system."""
    result = json.loads(await Shell.which_command(command="ls"))

    assert result["ok"] is True
    assert result["exists"] is True
    assert result["path"]  # non-empty path string


async def test_which_command_not_found_returns_error() -> None:
    result = json.loads(
        await Shell.which_command(command="definitely_not_a_real_cmd_xyz_obscura_123")
    )
    assert result["ok"] is False


async def test_which_command_empty_string_returns_error() -> None:
    result = json.loads(await Shell.which_command(command=""))
    assert result["ok"] is False
