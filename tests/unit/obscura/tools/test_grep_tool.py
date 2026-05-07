"""Unit tests for Grep.grep_files — Python fallback + mocked ripgrep.

Policy bypass: autouse fixture sets OBSCURA_UNSAFE_FULL_ACCESS=1.
Python-fallback tests use real tmp_path files; ripgrep tests patch
asyncio.create_subprocess_exec so no rg binary is required.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import obscura.tools.system._grep as _grep_mod

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _full_access(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OBSCURA_UNSAFE_FULL_ACCESS", "1")


@pytest.fixture
def _python_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force Python fallback by making shutil.which return None."""
    monkeypatch.setattr(_grep_mod.shutil, "which", lambda *a: None)


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
    return proc


# ---------------------------------------------------------------------------
# Python fallback — content mode
# ---------------------------------------------------------------------------


async def test_grep_python_content_mode_finds_match(
    tmp_path: Path, _python_fallback: None
) -> None:
    from obscura.tools.system._grep import Grep

    (tmp_path / "target.py").write_text("hello world\nno match here\n")

    result = json.loads(
        await Grep.grep_files(pattern="hello", path=str(tmp_path), output_mode="content")
    )

    assert result["ok"] is True
    assert result["mode"] == "content"
    assert result["count"] >= 1
    assert any("hello" in m["text"] for m in result["matches"])


async def test_grep_python_files_with_matches_mode(
    tmp_path: Path, _python_fallback: None
) -> None:
    from obscura.tools.system._grep import Grep

    (tmp_path / "match.py").write_text("target_token\n")
    (tmp_path / "nomatch.txt").write_text("nothing here\n")

    result = json.loads(
        await Grep.grep_files(
            pattern="target_token", path=str(tmp_path), output_mode="files_with_matches"
        )
    )

    assert result["ok"] is True
    assert result["mode"] == "files_with_matches"
    assert result["count"] == 1
    files = result["files"]
    assert len(files) == 1
    assert "match.py" in files[0]


async def test_grep_python_count_mode(
    tmp_path: Path, _python_fallback: None
) -> None:
    from obscura.tools.system._grep import Grep

    (tmp_path / "data.txt").write_text("needle\nneedle\nneedle\n")

    result = json.loads(
        await Grep.grep_files(pattern="needle", path=str(tmp_path), output_mode="count")
    )

    assert result["ok"] is True
    assert result["mode"] == "count"
    assert result["total_matches"] == 3
    assert result["num_files"] == 1


async def test_grep_python_case_insensitive(
    tmp_path: Path, _python_fallback: None
) -> None:
    from obscura.tools.system._grep import Grep

    (tmp_path / "f.txt").write_text("UPPER CASE WORD\n")

    result = json.loads(
        await Grep.grep_files(
            pattern="upper case", path=str(tmp_path), case_sensitive=False
        )
    )

    assert result["ok"] is True
    assert result["count"] >= 1


async def test_grep_python_no_matches_returns_empty(
    tmp_path: Path, _python_fallback: None
) -> None:
    from obscura.tools.system._grep import Grep

    (tmp_path / "f.txt").write_text("apple banana cherry\n")

    result = json.loads(
        await Grep.grep_files(pattern="zzz_no_such_token", path=str(tmp_path))
    )

    assert result["ok"] is True
    assert result["count"] == 0
    assert result["matches"] == []
    assert result["truncated"] is False


async def test_grep_python_invalid_regex_returns_error(
    tmp_path: Path, _python_fallback: None
) -> None:
    from obscura.tools.system._grep import Grep

    (tmp_path / "f.txt").write_text("anything\n")

    result = json.loads(
        await Grep.grep_files(pattern="[unclosed", path=str(tmp_path))
    )

    assert result["ok"] is False
    assert "invalid_regex" in result.get("error", "")


async def test_grep_python_head_limit_caps_results(
    tmp_path: Path, _python_fallback: None
) -> None:
    from obscura.tools.system._grep import Grep

    (tmp_path / "f.txt").write_text("\n".join(f"match {i}" for i in range(20)) + "\n")

    result = json.loads(
        await Grep.grep_files(pattern="match", path=str(tmp_path), head_limit=3)
    )

    assert result["ok"] is True
    assert len(result["matches"]) == 3
    assert result["truncated"] is True


async def test_grep_python_offset_paginates(
    tmp_path: Path, _python_fallback: None
) -> None:
    from obscura.tools.system._grep import Grep

    (tmp_path / "f.txt").write_text("\n".join(f"line {i}" for i in range(10)) + "\n")

    all_r = json.loads(
        await Grep.grep_files(pattern="line", path=str(tmp_path), head_limit=100)
    )
    paged_r = json.loads(
        await Grep.grep_files(
            pattern="line", path=str(tmp_path), offset=3, head_limit=2
        )
    )

    assert paged_r["ok"] is True
    assert len(paged_r["matches"]) == 2
    # offset skips first 3; paged result starts where full[3] begins
    assert paged_r["matches"][0]["text"] == all_r["matches"][3]["text"]


async def test_grep_python_nonexistent_path_returns_error(
    _python_fallback: None,
) -> None:
    from obscura.tools.system._grep import Grep

    result = json.loads(
        await Grep.grep_files(
            pattern="anything", path="/no/such/path/xyz_obscura_test_42"
        )
    )

    assert result["ok"] is False


async def test_grep_python_binary_extension_skipped(
    tmp_path: Path, _python_fallback: None
) -> None:
    from obscura.tools.system._grep import Grep

    # Write a .pyc file — Python fallback should skip it
    (tmp_path / "compiled.pyc").write_bytes(b"match this binary content match")

    result = json.loads(
        await Grep.grep_files(pattern="match", path=str(tmp_path))
    )

    assert result["ok"] is True
    # Binary file skipped → 0 matches (or at least no .pyc match)
    pyc_matches = [m for m in result.get("matches", []) if m["file"].endswith(".pyc")]
    assert pyc_matches == []


# ---------------------------------------------------------------------------
# Ripgrep mock tests
# ---------------------------------------------------------------------------


async def test_grep_ripgrep_content_mode_parses_output(tmp_path: Path) -> None:
    from obscura.tools.system._grep import Grep

    # Write a real file so the path-existence check passes
    (tmp_path / "src.py").write_text("hello world\n")
    stdout = b"src.py:1:hello world\n"
    proc = _fake_proc(stdout=stdout)

    with (
        patch.object(_grep_mod.shutil, "which", return_value="/usr/bin/rg"),
        patch.object(
            _grep_mod.asyncio,
            "create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ),
    ):
        result = json.loads(
            await Grep.grep_files(pattern="hello", path=str(tmp_path))
        )

    assert result["ok"] is True
    assert result["count"] >= 1
    assert any("hello" in m["text"] for m in result["matches"])


async def test_grep_ripgrep_files_with_matches_mode(tmp_path: Path) -> None:
    from obscura.tools.system._grep import Grep

    (tmp_path / "foo.py").write_text("token\n")
    stdout = (str(tmp_path / "foo.py") + "\n").encode()
    proc = _fake_proc(stdout=stdout)

    with (
        patch.object(_grep_mod.shutil, "which", return_value="/usr/bin/rg"),
        patch.object(
            _grep_mod.asyncio,
            "create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ),
    ):
        result = json.loads(
            await Grep.grep_files(
                pattern="token", path=str(tmp_path), output_mode="files_with_matches"
            )
        )

    assert result["ok"] is True
    assert result["mode"] == "files_with_matches"
    assert result["count"] >= 1


async def test_grep_ripgrep_timeout_returns_error(tmp_path: Path) -> None:
    from obscura.tools.system._grep import Grep

    (tmp_path / "f.txt").write_text("x\n")
    proc = _fake_proc()
    proc.communicate = AsyncMock(side_effect=TimeoutError)
    proc.wait = AsyncMock()

    with (
        patch.object(_grep_mod.shutil, "which", return_value="/usr/bin/rg"),
        patch.object(
            _grep_mod.asyncio,
            "create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ),
    ):
        result = json.loads(
            await Grep.grep_files(pattern="x", path=str(tmp_path))
        )

    assert result["ok"] is False


async def test_grep_ripgrep_case_insensitive_flag(tmp_path: Path) -> None:
    from obscura.tools.system._grep import Grep

    (tmp_path / "f.txt").write_text("UPPER\n")
    proc = _fake_proc(stdout=b"")

    with (
        patch.object(_grep_mod.shutil, "which", return_value="/usr/bin/rg"),
        patch.object(
            _grep_mod.asyncio,
            "create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ) as mock_exec,
    ):
        await Grep.grep_files(pattern="upper", path=str(tmp_path), case_sensitive=False)

    call_args = mock_exec.call_args[0]
    assert "-i" in call_args


async def test_grep_ripgrep_head_limit_truncates(tmp_path: Path) -> None:
    from obscura.tools.system._grep import Grep

    (tmp_path / "f.txt").write_text("x\n")
    # Generate 300 fake rg output lines
    lines = b"".join(
        f"f.txt:{i}:match {i}\n".encode() for i in range(1, 301)
    )
    proc = _fake_proc(stdout=lines)

    with (
        patch.object(_grep_mod.shutil, "which", return_value="/usr/bin/rg"),
        patch.object(
            _grep_mod.asyncio,
            "create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ),
    ):
        result = json.loads(
            await Grep.grep_files(
                pattern="match", path=str(tmp_path), head_limit=250
            )
        )

    assert result["ok"] is True
    assert result["truncated"] is True
    assert len(result["matches"]) <= 250
