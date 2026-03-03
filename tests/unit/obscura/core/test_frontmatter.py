"""Tests for YAML frontmatter parsing."""

from __future__ import annotations

from pathlib import Path

import pytest

from obscura.core.frontmatter import parse_frontmatter, parse_frontmatter_file


class TestParseFrontmatter:
    def test_standard_frontmatter(self) -> None:
        text = "---\nname: dev\nmodel: claude\n---\nYou are a dev agent."
        result = parse_frontmatter(text)
        assert result.metadata == {"name": "dev", "model": "claude"}
        assert result.body == "You are a dev agent."

    def test_no_frontmatter(self) -> None:
        text = "Just plain markdown."
        result = parse_frontmatter(text)
        assert result.metadata == {}
        assert result.body == "Just plain markdown."

    def test_empty_text(self) -> None:
        result = parse_frontmatter("")
        assert result.metadata == {}
        assert result.body == ""

    def test_whitespace_only(self) -> None:
        result = parse_frontmatter("   \n\n  ")
        assert result.metadata == {}
        assert result.body == ""

    def test_empty_frontmatter(self) -> None:
        text = "---\n---\nBody text."
        result = parse_frontmatter(text)
        assert result.metadata == {}
        assert result.body == "Body text."

    def test_malformed_yaml(self) -> None:
        text = "---\n: invalid: yaml: [[\n---\nBody."
        result = parse_frontmatter(text)
        assert result.metadata == {}
        assert result.body == "Body."

    def test_non_dict_yaml(self) -> None:
        text = "---\n- item1\n- item2\n---\nBody."
        result = parse_frontmatter(text)
        assert result.metadata == {}
        assert result.body == "Body."

    def test_nested_yaml(self) -> None:
        text = "---\nname: dev\npermissions:\n  allow:\n    - Read\n    - Write\n---\nPrompt."
        result = parse_frontmatter(text)
        assert result.metadata["name"] == "dev"
        assert result.metadata["permissions"] == {"allow": ["Read", "Write"]}

    def test_list_values(self) -> None:
        text = "---\ntools:\n  - Read\n  - Bash\n---\nBody."
        result = parse_frontmatter(text)
        assert result.metadata["tools"] == ["Read", "Bash"]

    def test_multiline_body(self) -> None:
        text = "---\nname: test\n---\nLine 1.\n\nLine 2.\n\nLine 3."
        result = parse_frontmatter(text)
        assert "Line 1." in result.body
        assert "Line 3." in result.body

    def test_source_path_preserved(self) -> None:
        p = Path("/tmp/test.md")
        result = parse_frontmatter("no frontmatter", source_path=p)
        assert result.source_path == p

    def test_body_with_dashes_not_confused(self) -> None:
        """Dashes in the body after frontmatter are not confused."""
        text = "---\nname: x\n---\nSome text.\n\n---\n\nMore text."
        result = parse_frontmatter(text)
        assert result.metadata == {"name": "x"}
        assert "---" in result.body
        assert "More text." in result.body


class TestParseFrontmatterFile:
    def test_reads_file(self, tmp_path: Path) -> None:
        f = tmp_path / "test.agent.md"
        f.write_text("---\nname: dev\n---\nSystem prompt.", encoding="utf-8")
        result = parse_frontmatter_file(f)
        assert result.metadata == {"name": "dev"}
        assert result.body == "System prompt."
        assert result.source_path == f

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            parse_frontmatter_file(tmp_path / "missing.md")
