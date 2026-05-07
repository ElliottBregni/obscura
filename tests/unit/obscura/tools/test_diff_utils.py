"""Unit tests for compute_unified_diff — pure Python, no mocks needed.

Each test passes string content directly; no subprocess or filesystem
I/O is involved so the tests run entirely in-process.
"""
from __future__ import annotations

import pytest

from obscura.tools.system.diff_utils import compute_unified_diff

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _diff(old: str, new: str, path: str = "file.py", **kw: object) -> dict:  # type: ignore[type-arg]
    return compute_unified_diff(old, new, path, **kw)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Identical input — no changes
# ---------------------------------------------------------------------------


def test_identical_strings_produce_no_hunks() -> None:
    result = _diff("abc\ndef\n", "abc\ndef\n")
    assert result["hunks"] == []
    assert result["insertions"] == 0
    assert result["deletions"] == 0


def test_identical_empty_strings_no_hunks() -> None:
    result = _diff("", "")
    assert result["hunks"] == []
    assert result["insertions"] == 0
    assert result["deletions"] == 0


def test_identical_strings_summary_shows_zero_counts() -> None:
    result = _diff("x\n", "x\n")
    assert "0" in result["summary"]


# ---------------------------------------------------------------------------
# Only insertions
# ---------------------------------------------------------------------------


def test_empty_old_to_content_counts_insertions() -> None:
    result = _diff("", "hello\nworld\n")
    assert result["insertions"] == 2
    assert result["deletions"] == 0


def test_single_line_added() -> None:
    result = _diff("a\nb\n", "a\nnew\nb\n")
    assert result["insertions"] == 1
    assert result["deletions"] == 0


# ---------------------------------------------------------------------------
# Only deletions
# ---------------------------------------------------------------------------


def test_content_to_empty_counts_deletions() -> None:
    result = _diff("a\nb\n", "")
    assert result["insertions"] == 0
    assert result["deletions"] == 2


def test_single_line_deleted() -> None:
    result = _diff("a\nremove_me\nb\n", "a\nb\n")
    assert result["insertions"] == 0
    assert result["deletions"] == 1


# ---------------------------------------------------------------------------
# Substitution
# ---------------------------------------------------------------------------


def test_single_line_substitution() -> None:
    result = _diff("old\n", "new\n")
    assert result["insertions"] == 1
    assert result["deletions"] == 1


# ---------------------------------------------------------------------------
# Plural / singular summary
# ---------------------------------------------------------------------------


def test_summary_singular_insertion() -> None:
    result = _diff("a\n", "a\nnew\n")
    assert "1 insertion" in result["summary"]
    assert "insertions" not in result["summary"].replace("1 insertion", "")


def test_summary_singular_deletion() -> None:
    result = _diff("a\nremove\n", "a\n")
    assert "1 deletion" in result["summary"]


def test_summary_plural_insertions() -> None:
    result = _diff("", "a\nb\n")
    assert "2 insertions" in result["summary"]


# ---------------------------------------------------------------------------
# Hunk structure
# ---------------------------------------------------------------------------


def test_hunk_header_contains_at_signs() -> None:
    result = _diff("a\n", "b\n")
    assert len(result["hunks"]) >= 1
    assert result["hunks"][0]["header"].startswith("@@")


def test_multi_line_change_produces_hunk_lines() -> None:
    result = _diff("a\nb\n", "a\nc\n")
    assert any(result["hunks"])
    all_lines = [l for h in result["hunks"] for l in h["lines"]]
    assert any(l.startswith("-") for l in all_lines)
    assert any(l.startswith("+") for l in all_lines)


# ---------------------------------------------------------------------------
# context_lines parameter
# ---------------------------------------------------------------------------


def test_context_lines_zero_strips_surrounding_context() -> None:
    # With 5 unchanged lines around the change, context=0 means no surrounding lines
    old = "a\nb\nc\nCHANGE\nd\ne\nf\n"
    new = "a\nb\nc\nNEW\nd\ne\nf\n"
    result = _diff(old, new, context_lines=0)
    all_lines = [l for h in result["hunks"] for l in h["lines"]]
    # Should only contain the - and + lines, no surrounding context
    for line in all_lines:
        assert line.startswith("-") or line.startswith("+")


# ---------------------------------------------------------------------------
# unified output
# ---------------------------------------------------------------------------


def test_unified_field_contains_at_at_header() -> None:
    result = _diff("old\n", "new\n")
    assert "@@" in result["unified"]


def test_path_echoed_in_unified() -> None:
    result = _diff("x\n", "y\n", path="src/foo.py")
    assert "src/foo.py" in result["unified"]


def test_unified_field_is_non_empty_for_real_change() -> None:
    result = _diff("a\n", "b\n")
    assert isinstance(result["unified"], str)
    assert len(result["unified"]) > 0
