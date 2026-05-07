"""Unit tests for read_team_prompt.

Single async @tool function. Patches resolve_obscura_home to tmp_path so
~/.obscura is never touched.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

import obscura.tools.system.team_prompt as _tp_mod

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# read_team_prompt
# ---------------------------------------------------------------------------


async def test_read_team_prompt_no_file_returns_not_found(tmp_path: Path) -> None:
    from obscura.tools.system.team_prompt import read_team_prompt

    with patch.object(_tp_mod, "resolve_obscura_home", return_value=tmp_path):
        result = json.loads(await read_team_prompt())

    assert result["ok"] is False
    assert result["error"] == "team_prompt_not_found"
    assert "filenames_tried" in result


async def test_read_team_prompt_finds_md_file(tmp_path: Path) -> None:
    from obscura.tools.system.team_prompt import read_team_prompt

    (tmp_path / "team_prompt.md").write_text("# Team Rules\n\nBe nice.")

    with patch.object(_tp_mod, "resolve_obscura_home", return_value=tmp_path):
        result = json.loads(await read_team_prompt())

    assert result["ok"] is True
    assert "Be nice" in result["text"]
    assert result["path"].endswith("team_prompt.md")


async def test_read_team_prompt_finds_txt_file(tmp_path: Path) -> None:
    from obscura.tools.system.team_prompt import read_team_prompt

    (tmp_path / "team_prompt.txt").write_text("Plain text prompt.")

    with patch.object(_tp_mod, "resolve_obscura_home", return_value=tmp_path):
        result = json.loads(await read_team_prompt())

    assert result["ok"] is True
    assert "Plain text prompt" in result["text"]


async def test_read_team_prompt_explicit_path_returns_file(tmp_path: Path) -> None:
    from obscura.tools.system.team_prompt import read_team_prompt

    f = tmp_path / "custom_prompt.md"
    f.write_text("Custom content here.")

    result = json.loads(await read_team_prompt(path=str(f)))

    assert result["ok"] is True
    assert "Custom content" in result["text"]


async def test_read_team_prompt_explicit_path_missing_returns_error(
    tmp_path: Path,
) -> None:
    from obscura.tools.system.team_prompt import read_team_prompt

    result = json.loads(await read_team_prompt(path=str(tmp_path / "no_such.md")))

    assert result["ok"] is False
    assert result["error"] == "path_not_found"


async def test_read_team_prompt_explicit_path_is_directory_returns_error(
    tmp_path: Path,
) -> None:
    from obscura.tools.system.team_prompt import read_team_prompt

    result = json.loads(await read_team_prompt(path=str(tmp_path)))

    assert result["ok"] is False
    assert result["error"] == "not_a_file"


async def test_read_team_prompt_prefers_md_over_txt(tmp_path: Path) -> None:
    from obscura.tools.system.team_prompt import read_team_prompt

    (tmp_path / "team_prompt.md").write_text("Markdown wins.")
    (tmp_path / "team_prompt.txt").write_text("Text loses.")

    with patch.object(_tp_mod, "resolve_obscura_home", return_value=tmp_path):
        result = json.loads(await read_team_prompt())

    assert "Markdown wins" in result["text"]
