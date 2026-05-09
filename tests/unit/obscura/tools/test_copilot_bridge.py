"""Unit tests for copilot_bridge — view, edit, grep, glob, report_intent.

All five handler functions are sync. Tests use:
  - Real tmp_path files for view/edit (no subprocess needed for file I/O)
  - subprocess.run mocks for grep and glob (rg/fd/find binaries not required)
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import obscura.tools.copilot_bridge as _bridge_mod

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# view_file_impl
# ---------------------------------------------------------------------------


def test_view_file_nonexistent_returns_error(tmp_path: Path) -> None:
    from obscura.tools.copilot_bridge import view_file_impl

    result = view_file_impl(str(tmp_path / "no_such.txt"))

    assert "Error" in result
    assert "does not exist" in result


def test_view_file_returns_numbered_lines(tmp_path: Path) -> None:
    from obscura.tools.copilot_bridge import view_file_impl

    f = tmp_path / "file.txt"
    f.write_text("alpha\nbeta\ngamma\n")

    result = view_file_impl(str(f))

    assert "1. alpha" in result
    assert "2. beta" in result
    assert "3. gamma" in result


def test_view_file_respects_start_and_end_line(tmp_path: Path) -> None:
    from obscura.tools.copilot_bridge import view_file_impl

    f = tmp_path / "file.txt"
    f.write_text("line1\nline2\nline3\nline4\n")

    result = view_file_impl(str(f), start_line=2, end_line=3)

    assert "2. line2" in result
    assert "3. line3" in result
    assert "line1" not in result
    assert "line4" not in result


def test_view_file_directory_calls_ls(tmp_path: Path) -> None:
    from obscura.tools.copilot_bridge import view_file_impl

    proc = MagicMock()
    proc.stdout = "total 0\ndrwxr-xr-x  2 user  staff  64 Jan  1 00:00 .\n"

    with patch.object(_bridge_mod.subprocess, "run", return_value=proc) as mock_run:
        view_file_impl(str(tmp_path))

    mock_run.assert_called_once()
    cmd = mock_run.call_args[0][0]
    assert cmd[0] == "ls"
    assert "-la" in cmd


# ---------------------------------------------------------------------------
# edit_file_impl
# ---------------------------------------------------------------------------


def test_edit_file_nonexistent_returns_error(tmp_path: Path) -> None:
    from obscura.tools.copilot_bridge import edit_file_impl

    result = edit_file_impl(str(tmp_path / "missing.py"), "old", "new")

    assert "Error" in result
    assert "does not exist" in result


def test_edit_file_old_str_not_found_returns_error(tmp_path: Path) -> None:
    from obscura.tools.copilot_bridge import edit_file_impl

    f = tmp_path / "file.py"
    f.write_text("alpha beta\n")

    result = edit_file_impl(str(f), "not_there", "replacement")

    assert "not found" in result


def test_edit_file_old_str_appears_multiple_times_returns_error(tmp_path: Path) -> None:
    from obscura.tools.copilot_bridge import edit_file_impl

    f = tmp_path / "file.py"
    f.write_text("dup dup\n")

    result = edit_file_impl(str(f), "dup", "new")

    assert "appears 2 times" in result
    assert "must be unique" in result


def test_edit_file_success_replaces_content(tmp_path: Path) -> None:
    from obscura.tools.copilot_bridge import edit_file_impl

    f = tmp_path / "file.py"
    f.write_text("hello world\n")

    result = edit_file_impl(str(f), "world", "obscura")

    assert "updated successfully" in result
    assert f.read_text() == "hello obscura\n"


def test_edit_file_replaces_only_first_occurrence(tmp_path: Path) -> None:
    """old_str must be unique — but if we force one occurrence, only that's replaced."""
    from obscura.tools.copilot_bridge import edit_file_impl

    f = tmp_path / "file.py"
    f.write_text("unique_token\nother content\n")

    edit_file_impl(str(f), "unique_token", "replaced")

    assert "replaced" in f.read_text()
    assert "unique_token" not in f.read_text()


# ---------------------------------------------------------------------------
# grep_impl
# ---------------------------------------------------------------------------


def test_grep_impl_calls_rg_with_pattern(tmp_path: Path) -> None:
    from obscura.tools.copilot_bridge import grep_impl

    proc = MagicMock()
    proc.returncode = 0
    proc.stdout = "src/main.py:10:def my_func():\n"
    proc.stderr = ""

    with patch.object(_bridge_mod.subprocess, "run", return_value=proc) as mock_run:
        result = grep_impl("my_func", str(tmp_path))

    cmd = mock_run.call_args[0][0]
    assert "rg" in cmd
    assert "my_func" in cmd
    assert result == proc.stdout


def test_grep_impl_case_insensitive_adds_flag(tmp_path: Path) -> None:
    from obscura.tools.copilot_bridge import grep_impl

    proc = MagicMock()
    proc.returncode = 0
    proc.stdout = ""
    proc.stderr = ""

    with patch.object(_bridge_mod.subprocess, "run", return_value=proc) as mock_run:
        grep_impl("pattern", str(tmp_path), case_insensitive=True)

    cmd = mock_run.call_args[0][0]
    assert "-i" in cmd


def test_grep_impl_no_matches_returns_no_matches_found(tmp_path: Path) -> None:
    from obscura.tools.copilot_bridge import grep_impl

    proc = MagicMock()
    proc.returncode = 1  # rg exits 1 when no matches
    proc.stdout = ""
    proc.stderr = ""

    with patch.object(_bridge_mod.subprocess, "run", return_value=proc):
        result = grep_impl("zzz", str(tmp_path))

    assert result == "No matches found"


# ---------------------------------------------------------------------------
# glob_impl
# ---------------------------------------------------------------------------


def test_glob_impl_calls_fd(tmp_path: Path) -> None:
    from obscura.tools.copilot_bridge import glob_impl

    proc = MagicMock()
    proc.returncode = 0
    proc.stdout = "src/main.py\nsrc/utils.py\n"

    with patch.object(_bridge_mod.subprocess, "run", return_value=proc) as mock_run:
        result = glob_impl("*.py", str(tmp_path))

    cmd = mock_run.call_args[0][0]
    assert "fd" in cmd
    assert "*.py" in cmd
    assert "src/main.py" in result


def test_glob_impl_falls_back_to_find_on_fd_failure(tmp_path: Path) -> None:
    from obscura.tools.copilot_bridge import glob_impl

    fail_proc = MagicMock()
    fail_proc.returncode = 1
    fail_proc.stdout = ""

    find_proc = MagicMock()
    find_proc.returncode = 0
    find_proc.stdout = "src/main.py\n"

    with patch.object(
        _bridge_mod.subprocess,
        "run",
        side_effect=[fail_proc, find_proc],
    ) as mock_run:
        result = glob_impl("*.py", str(tmp_path))

    assert mock_run.call_count == 2
    second_cmd = mock_run.call_args_list[1][0][0]
    assert "find" in second_cmd
    assert "src/main.py" in result


# ---------------------------------------------------------------------------
# report_intent_impl
# ---------------------------------------------------------------------------


def test_report_intent_returns_ok_with_intent() -> None:
    from obscura.tools.copilot_bridge import report_intent_impl

    result = json.loads(report_intent_impl("searching for the bug"))

    assert result["ok"] is True
    assert result["intent"] == "searching for the bug"


# ---------------------------------------------------------------------------
# make_copilot_bridge_tool_specs
# ---------------------------------------------------------------------------


def test_make_copilot_bridge_tool_specs_returns_four_specs() -> None:
    from obscura.tools.copilot_bridge import make_copilot_bridge_tool_specs

    specs = make_copilot_bridge_tool_specs(MagicMock())

    assert len(specs) == 4
    names = {s.name for s in specs}
    assert names == {"view", "edit", "grep", "glob"}
    for spec in specs:
        assert callable(spec.handler)
        assert isinstance(spec.parameters, dict)
