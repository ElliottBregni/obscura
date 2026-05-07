"""Unit tests for filesystem tools (read, write, edit, list, find, grep)."""
# pyright: reportPrivateUsage=false

from __future__ import annotations

import json
from pathlib import Path

import pytest

from obscura.tools.system._fs_read import FsRead
from obscura.tools.system._fs_write import FsWrite
from obscura.tools.system._grep import Grep

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _full_access(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bypass path-allow checks for all tests in this module."""
    monkeypatch.setenv("OBSCURA_UNSAFE_FULL_ACCESS", "1")


# ---------------------------------------------------------------------------
# list_directory
# ---------------------------------------------------------------------------


async def test_list_directory_returns_sorted_entries(tmp_path: Path) -> None:
    (tmp_path / "b.txt").write_text("hi")
    (tmp_path / "a.txt").write_text("there")
    (tmp_path / "subdir").mkdir()

    result = json.loads(await FsRead.list_directory(path=str(tmp_path)))

    assert result["ok"] is True
    names = [e["name"] for e in result["entries"]]
    assert names == sorted(names)
    assert set(names) == {"a.txt", "b.txt", "subdir"}


async def test_list_directory_not_found_returns_error() -> None:
    result = json.loads(await FsRead.list_directory(path="/absolutely/no/such/dir"))
    assert result["ok"] is False


async def test_list_directory_on_file_returns_error(tmp_path: Path) -> None:
    f = tmp_path / "file.txt"
    f.write_text("x")
    result = json.loads(await FsRead.list_directory(path=str(f)))
    assert result["ok"] is False


# ---------------------------------------------------------------------------
# read_text_file
# ---------------------------------------------------------------------------


async def test_read_text_file_success(tmp_path: Path) -> None:
    f = tmp_path / "hello.txt"
    f.write_text("world")

    result = json.loads(await FsRead.read_text_file(path=str(f)))

    assert result["ok"] is True
    assert "world" in result["text"]


async def test_read_text_file_not_found_returns_error() -> None:
    result = json.loads(await FsRead.read_text_file(path="/no/such/file.txt"))
    assert result["ok"] is False


async def test_read_text_file_offset_and_limit(tmp_path: Path) -> None:
    f = tmp_path / "lines.txt"
    f.write_text("\n".join(str(i) for i in range(10)))

    result = json.loads(await FsRead.read_text_file(path=str(f), offset=2, limit=3))

    assert result["ok"] is True
    lines = result["text"].splitlines()
    assert len(lines) <= 3


# ---------------------------------------------------------------------------
# write_text_file
# ---------------------------------------------------------------------------


async def test_write_text_file_creates_file(tmp_path: Path) -> None:
    target = str(tmp_path / "new.txt")

    result = json.loads(await FsWrite.write_text_file(path=target, text="content"))

    assert result["ok"] is True
    assert Path(target).read_text() == "content"


async def test_write_text_file_creates_parent_dirs(tmp_path: Path) -> None:
    target = str(tmp_path / "deep" / "nested" / "file.txt")

    result = json.loads(await FsWrite.write_text_file(path=target, text="hi"))

    assert result["ok"] is True
    assert Path(target).exists()


async def test_write_text_file_overwrites_by_default(tmp_path: Path) -> None:
    f = tmp_path / "existing.txt"
    f.write_text("old")

    result = json.loads(await FsWrite.write_text_file(path=str(f), text="new"))

    assert result["ok"] is True
    assert f.read_text() == "new"


# ---------------------------------------------------------------------------
# edit_text_file
# ---------------------------------------------------------------------------


async def test_edit_text_file_replaces_string(tmp_path: Path) -> None:
    f = tmp_path / "edit.txt"
    f.write_text("hello world")

    result = json.loads(
        await FsWrite.edit_text_file(path=str(f), old_text="world", new_text="there")
    )

    assert result["ok"] is True
    assert f.read_text() == "hello there"


async def test_edit_text_file_old_text_not_found(tmp_path: Path) -> None:
    f = tmp_path / "edit.txt"
    f.write_text("hello")

    result = json.loads(
        await FsWrite.edit_text_file(path=str(f), old_text="nope", new_text="x")
    )

    assert result["ok"] is False


async def test_edit_text_file_replace_all(tmp_path: Path) -> None:
    f = tmp_path / "multi.txt"
    f.write_text("a a a")

    result = json.loads(
        await FsWrite.edit_text_file(path=str(f), old_text="a", new_text="b", replace_all=True)
    )

    assert result["ok"] is True
    assert f.read_text() == "b b b"


# ---------------------------------------------------------------------------
# append_text_file
# ---------------------------------------------------------------------------


async def test_append_text_file_appends_to_existing(tmp_path: Path) -> None:
    f = tmp_path / "log.txt"
    f.write_text("line1\n")

    result = json.loads(await FsWrite.append_text_file(path=str(f), text="line2\n"))

    assert result["ok"] is True
    assert f.read_text() == "line1\nline2\n"


async def test_append_text_file_creates_new_file(tmp_path: Path) -> None:
    target = str(tmp_path / "brand_new.txt")

    result = json.loads(await FsWrite.append_text_file(path=target, text="first\n"))

    assert result["ok"] is True
    assert Path(target).read_text() == "first\n"


# ---------------------------------------------------------------------------
# find_files
# ---------------------------------------------------------------------------


async def test_find_files_glob_matches_by_extension(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("")
    (tmp_path / "b.txt").write_text("")
    (tmp_path / "c.py").write_text("")

    result = json.loads(await FsRead.find_files(path=str(tmp_path), pattern="*.py"))

    assert result["ok"] is True
    names = {r["name"] for r in result["results"]}
    assert names == {"a.py", "c.py"}


async def test_find_files_empty_dir(tmp_path: Path) -> None:
    result = json.loads(await FsRead.find_files(path=str(tmp_path), pattern="*.py"))
    assert result["ok"] is True
    assert result["results"] == []


# ---------------------------------------------------------------------------
# grep_files
# ---------------------------------------------------------------------------


async def test_grep_files_finds_match_in_content_mode(tmp_path: Path) -> None:
    (tmp_path / "code.py").write_text("def my_function():\n    pass\n")

    result = json.loads(
        await Grep.grep_files(
            pattern="my_function", path=str(tmp_path), output_mode="content"
        )
    )

    assert result["ok"] is True
    # content mode returns lines with matches
    matches = result.get("matches", [])
    assert any("my_function" in str(m) for m in matches)


async def test_grep_files_no_match_returns_empty(tmp_path: Path) -> None:
    (tmp_path / "code.py").write_text("hello world\n")

    result = json.loads(
        await Grep.grep_files(
            pattern="zzz_no_match_xyz", path=str(tmp_path), output_mode="content"
        )
    )

    assert result["ok"] is True
    assert result.get("results", []) == []
