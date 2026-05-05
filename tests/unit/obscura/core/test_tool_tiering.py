"""Tests for obscura.core.tool_tiering.

Verifies the core/deferred split, ordering preservation, and the
``deferred_listing`` rendering. Backend ``_build_tool_listing`` methods
import these primitives lazily; integration is covered by the existing
backend test suites.
"""

from __future__ import annotations

from typing import Any

from obscura.core.tool_tiering import (
    CORE_TOOL_NAMES,
    deferred_listing,
    is_core,
    split_by_tier,
)
from obscura.core.types import ToolSpec


def _stub_handler(*_a: Any, **_kw: Any) -> str:
    return ""


def _spec(name: str, description: str = "") -> ToolSpec:
    return ToolSpec(
        name=name,
        description=description or f"description for {name}",
        parameters={"type": "object", "properties": {}},
        handler=_stub_handler,
    )


def test_core_set_includes_essentials() -> None:
    """The core set must include the tools the model needs without discovery."""
    expected = {
        "read_text_file", "write_text_file", "grep_files",
        "run_shell", "which_command",
        "web_fetch", "web_search",
        "tool_search",  # MUST be core — entry point to the rest.
    }
    assert expected <= CORE_TOOL_NAMES


def test_is_core_round_trip() -> None:
    assert is_core("tool_search")
    assert is_core("read_text_file")
    assert not is_core("jira_create_issue")
    assert not is_core("definitely_not_a_real_tool")


def test_split_preserves_order_within_each_tier() -> None:
    tools = [
        _spec("read_text_file"),       # core
        _spec("jira_create_issue"),    # deferred
        _spec("grep_files"),           # core
        _spec("postman_run_collection"),  # deferred
        _spec("write_text_file"),      # core
    ]
    core, deferred = split_by_tier(tools)
    assert [s.name for s in core] == [
        "read_text_file", "grep_files", "write_text_file",
    ]
    assert [s.name for s in deferred] == [
        "jira_create_issue", "postman_run_collection",
    ]


def test_split_with_no_tools() -> None:
    core, deferred = split_by_tier([])
    assert core == []
    assert deferred == []


def test_split_all_core() -> None:
    tools = [_spec("read_text_file"), _spec("grep_files")]
    core, deferred = split_by_tier(tools)
    assert len(core) == 2
    assert deferred == []


def test_split_all_deferred() -> None:
    tools = [_spec("custom_a"), _spec("custom_b")]
    core, deferred = split_by_tier(tools)
    assert core == []
    assert len(deferred) == 2


def test_deferred_listing_renders_names_and_descriptions() -> None:
    tools = [
        _spec("jira_create_issue", "Create a Jira issue with given fields."),
        _spec("postman_run", "Execute a Postman collection."),
    ]
    output = deferred_listing(tools)
    assert "tool_search" in output  # instruction mentions discovery path
    assert "`jira_create_issue`" in output
    assert "Create a Jira issue with given fields." in output
    assert "`postman_run`" in output


def test_deferred_listing_truncates_long_descriptions() -> None:
    long = "x" * 500
    tools = [_spec("noisy_tool", long)]
    output = deferred_listing(tools, max_per_line=80)
    assert len([line for line in output.splitlines() if "noisy_tool" in line]) == 1
    # Truncation marker is present
    assert "…" in output


def test_deferred_listing_uses_first_line_only() -> None:
    multi = "First-line summary.\nDetailed second line should not appear."
    tools = [_spec("multi_line_tool", multi)]
    output = deferred_listing(tools)
    assert "First-line summary." in output
    assert "Detailed second line" not in output


def test_deferred_listing_empty_input() -> None:
    assert deferred_listing([]) == ""
