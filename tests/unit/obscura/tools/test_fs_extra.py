"""Unit tests for additional filesystem tools:
copy_path, move_path, remove_path, diff_files (FsWrite)
tree_directory, file_info (FsRead)."""
# pyright: reportPrivateUsage=false

from __future__ import annotations

import json
from pathlib import Path

import pytest

from obscura.tools.system._fs_read import FsRead
from obscura.tools.system._fs_write import FsWrite

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _full_access(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bypass path-allow checks for all tests in this module."""
    monkeypatch.setenv("OBSCURA_UNSAFE_FULL_ACCESS", "1")


# ---------------------------------------------------------------------------
# copy_path
# ---------------------------------------------------------------------------


async def test_copy_path_copies_file(tmp_path: Path) -> None:
    src = tmp_path / "source.txt"
    src.write_text("original content")
    dst = str(tmp_path / "dest.txt")

    result = json.loads(await FsWrite.copy_path(src=str(src), dst=dst))

    assert result["ok"] is True
    assert Path(dst).read_text() == "original content"
    assert src.exists()  # source is preserved


async def test_copy_path_no_overwrite_returns_error(tmp_path: Path) -> None:
    src = tmp_path / "a.txt"
    src.write_text("a")
    dst = tmp_path / "b.txt"
    dst.write_text("b")

    result = json.loads(await FsWrite.copy_path(src=str(src), dst=str(dst)))

    assert result["ok"] is False


async def test_copy_path_overwrite_replaces_destination(tmp_path: Path) -> None:
    src = tmp_path / "new.txt"
    src.write_text("new content")
    dst = tmp_path / "old.txt"
    dst.write_text("old content")

    result = json.loads(
        await FsWrite.copy_path(src=str(src), dst=str(dst), overwrite=True)
    )

    assert result["ok"] is True
    assert dst.read_text() == "new content"


async def test_copy_path_source_not_found(tmp_path: Path) -> None:
    result = json.loads(
        await FsWrite.copy_path(
            src=str(tmp_path / "no_such.txt"), dst=str(tmp_path / "out.txt")
        )
    )
    assert result["ok"] is False


# ---------------------------------------------------------------------------
# move_path
# ---------------------------------------------------------------------------


async def test_move_path_moves_file(tmp_path: Path) -> None:
    src = tmp_path / "from.txt"
    src.write_text("data")
    dst = str(tmp_path / "to.txt")

    result = json.loads(await FsWrite.move_path(src=str(src), dst=dst))

    assert result["ok"] is True
    assert Path(dst).read_text() == "data"
    assert not src.exists()  # source gone


async def test_move_path_no_overwrite_returns_error(tmp_path: Path) -> None:
    src = tmp_path / "src.txt"
    src.write_text("x")
    dst = tmp_path / "dst.txt"
    dst.write_text("y")

    result = json.loads(await FsWrite.move_path(src=str(src), dst=str(dst)))

    assert result["ok"] is False


# ---------------------------------------------------------------------------
# remove_path
# ---------------------------------------------------------------------------


async def test_remove_path_deletes_file(tmp_path: Path) -> None:
    f = tmp_path / "to_delete.txt"
    f.write_text("bye")

    result = json.loads(await FsWrite.remove_path(path=str(f)))

    assert result["ok"] is True
    assert not f.exists()


async def test_remove_path_missing_ok_true_returns_ok(tmp_path: Path) -> None:
    result = json.loads(
        await FsWrite.remove_path(
            path=str(tmp_path / "ghost.txt"), missing_ok=True
        )
    )
    assert result["ok"] is True


async def test_remove_path_missing_ok_false_returns_error(tmp_path: Path) -> None:
    result = json.loads(
        await FsWrite.remove_path(
            path=str(tmp_path / "ghost.txt"), missing_ok=False
        )
    )
    assert result["ok"] is False


async def test_remove_path_directory_without_recursive_returns_error(
    tmp_path: Path,
) -> None:
    d = tmp_path / "subdir"
    d.mkdir()

    result = json.loads(await FsWrite.remove_path(path=str(d), recursive=False))

    assert result["ok"] is False


async def test_remove_path_directory_recursive_succeeds(tmp_path: Path) -> None:
    d = tmp_path / "rmdir"
    d.mkdir()
    (d / "child.txt").write_text("x")

    result = json.loads(await FsWrite.remove_path(path=str(d), recursive=True))

    assert result["ok"] is True
    assert not d.exists()


# ---------------------------------------------------------------------------
# diff_files
# ---------------------------------------------------------------------------


async def test_diff_files_identical_files(tmp_path: Path) -> None:
    f1 = tmp_path / "a.txt"
    f2 = tmp_path / "b.txt"
    f1.write_text("same content\n")
    f2.write_text("same content\n")

    result = json.loads(await FsWrite.diff_files(file_a=str(f1), file_b=str(f2)))

    assert result["ok"] is True
    assert result["identical"] is True
    assert result["diff"] == ""


async def test_diff_files_different_files(tmp_path: Path) -> None:
    f1 = tmp_path / "old.txt"
    f2 = tmp_path / "new.txt"
    f1.write_text("line one\nline two\n")
    f2.write_text("line one\nline THREE\n")

    result = json.loads(await FsWrite.diff_files(file_a=str(f1), file_b=str(f2)))

    assert result["ok"] is True
    assert result["identical"] is False
    assert "-line two" in result["diff"] or "+line THREE" in result["diff"]


async def test_diff_files_missing_file_returns_error(tmp_path: Path) -> None:
    f1 = tmp_path / "exists.txt"
    f1.write_text("hi")

    result = json.loads(
        await FsWrite.diff_files(
            file_a=str(f1), file_b=str(tmp_path / "missing.txt")
        )
    )

    assert result["ok"] is False


# ---------------------------------------------------------------------------
# tree_directory
# ---------------------------------------------------------------------------


async def test_tree_directory_returns_tree_string(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("")
    (tmp_path / "b.txt").write_text("")
    (tmp_path / "sub").mkdir()

    result = json.loads(await FsRead.tree_directory(path=str(tmp_path)))

    assert result["ok"] is True
    assert isinstance(result["tree"], str)
    assert len(result["tree"]) > 0
    assert result["entries"] >= 3


async def test_tree_directory_not_found_returns_error() -> None:
    result = json.loads(await FsRead.tree_directory(path="/absolutely/no/such/dir"))
    assert result["ok"] is False


async def test_tree_directory_max_depth_limits_output(tmp_path: Path) -> None:
    deep = tmp_path / "level1" / "level2"
    deep.mkdir(parents=True)
    (deep / "deep_file.txt").write_text("")

    result = json.loads(await FsRead.tree_directory(path=str(tmp_path), max_depth=1))

    assert result["ok"] is True
    # deep_file.txt is 2 levels down — should NOT appear in a max_depth=1 tree
    assert "deep_file.txt" not in result["tree"]


async def test_tree_directory_on_file_returns_error(tmp_path: Path) -> None:
    f = tmp_path / "file.txt"
    f.write_text("x")
    result = json.loads(await FsRead.tree_directory(path=str(f)))
    assert result["ok"] is False


# ---------------------------------------------------------------------------
# file_info
# ---------------------------------------------------------------------------


async def test_file_info_returns_metadata(tmp_path: Path) -> None:
    f = tmp_path / "sample.py"
    f.write_text("print('hello')")

    result = json.loads(await FsRead.file_info(path=str(f)))

    assert result["ok"] is True
    info = result["info"]
    assert info["name"] == "sample.py"
    assert info["is_file"] is True
    assert info["is_dir"] is False
    assert info["size"] > 0
    assert info["extension"] == ".py"


async def test_file_info_on_directory(tmp_path: Path) -> None:
    d = tmp_path / "mydir"
    d.mkdir()

    result = json.loads(await FsRead.file_info(path=str(d)))

    assert result["ok"] is True
    info = result["info"]
    assert info["is_dir"] is True
    assert info["is_file"] is False


async def test_file_info_not_found_returns_error() -> None:
    result = json.loads(await FsRead.file_info(path="/no/such/path/ever.txt"))
    assert result["ok"] is False
