from pathlib import Path

import pytest

from obscura.integrations.mcp.file_tools import _resolve_safe, search_files


def test_resolve_safe_rejects_sibling_with_shared_prefix(tmp_path: Path) -> None:
    allowed_root = tmp_path / "allowed"
    sibling = tmp_path / "allowed-sibling"
    allowed_root.mkdir()
    sibling.mkdir()
    secret = sibling / "note.txt"
    secret.write_text("outside", encoding="utf-8")

    with pytest.raises(ValueError, match="outside allowed directories"):
        _resolve_safe(str(secret), allowed_roots=[str(allowed_root)])


def test_resolve_safe_allows_root_itself(tmp_path: Path) -> None:
    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir()

    assert _resolve_safe(str(allowed_root), allowed_roots=[str(allowed_root)]) == allowed_root


def test_search_files_rejects_sibling_root_with_shared_prefix(tmp_path: Path) -> None:
    allowed_root = tmp_path / "allowed"
    sibling = tmp_path / "allowed-sibling"
    allowed_root.mkdir()
    sibling.mkdir()
    (sibling / "note.txt").write_text("needle", encoding="utf-8")

    result = search_files(
        "needle",
        root=str(sibling),
        allowed_roots=[str(allowed_root)],
    )

    assert result == {
        "error": f"Search root {str(sibling)!r} is outside allowed directories",
    }
