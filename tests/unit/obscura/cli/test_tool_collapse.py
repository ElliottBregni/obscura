"""Tests for obscura.cli.tool_collapse — output collapsing."""

from __future__ import annotations

from obscura.cli.tool_collapse import ToolCollapser


def test_collapsible_tools_are_recorded() -> None:
    c = ToolCollapser()
    assert c.record("read_text_file", {"path": "foo.py"})
    assert c.record("grep_files", {"pattern": "TODO", "path": "."})
    assert c.count == 2


def test_non_collapsible_not_recorded() -> None:
    c = ToolCollapser()
    assert not c.record("run_shell", {"script": "ls"})
    assert not c.record("write_text_file", {"path": "x", "text": "y"})
    assert c.count == 0


def test_flush_summary() -> None:
    c = ToolCollapser()
    c.record("read_text_file", {"path": "a.py"})
    c.record("read_text_file", {"path": "b.py"})
    c.record("grep_files", {"pattern": "foo"})
    summary = c.flush_summary()
    assert "Read x2" in summary
    assert "Grep" in summary
    assert c.count == 0  # cleared after flush


def test_flush_empty() -> None:
    c = ToolCollapser()
    assert c.flush_summary() == ""


def test_pending_flag() -> None:
    c = ToolCollapser()
    assert not c.pending
    c.record("read_text_file", {"path": "x"})
    assert c.pending
    c.flush_summary()
    assert not c.pending


def test_detail_extraction() -> None:
    c = ToolCollapser()
    c.record("web_search", {"query": "python asyncio"})
    details = c.flush_details()
    assert len(details) == 1
    assert "python asyncio" in details[0]
