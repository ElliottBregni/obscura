"""Tests for obscura.core.tool_tiering.

Verifies the core/deferred split, ordering preservation, the
``deferred_listing`` rendering, and the per-task discovery set used by
backends to filter their per-turn tool payloads.
"""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import patch

from obscura.core.tool_tiering import (
    CORE_TOOL_NAMES,
    DISCOVERED_TOOLS,
    bind_discovered_tools,
    deferred_listing,
    effective_core_names,
    filter_visible,
    is_core,
    is_effectively_core,
    is_phase3_active,
    is_visible,
    mark_discovered,
    parse_extra_core_patterns,
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
        "read_text_file",
        "write_text_file",
        "grep_files",
        "run_shell",
        "which_command",
        "web_fetch",
        "web_search",
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
        _spec("read_text_file"),  # core
        _spec("jira_create_issue"),  # deferred
        _spec("grep_files"),  # core
        _spec("postman_run_collection"),  # deferred
        _spec("write_text_file"),  # core
    ]
    core, deferred = split_by_tier(tools)
    assert [s.name for s in core] == [
        "read_text_file",
        "grep_files",
        "write_text_file",
    ]
    assert [s.name for s in deferred] == [
        "jira_create_issue",
        "postman_run_collection",
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


# ---------------------------------------------------------------------------
# Per-task discovery (DISCOVERED_TOOLS / mark_discovered / is_visible)
# ---------------------------------------------------------------------------


def _reset_discovered() -> None:
    """Tests start with no discovery context bound."""
    DISCOVERED_TOOLS.set(None)


def test_is_visible_no_context_passes_everything() -> None:
    """Outside ``bind_discovered_tools``, every tool is treated as visible."""
    _reset_discovered()
    assert is_visible("read_text_file") is True  # core
    assert is_visible("jira_create_issue") is True  # deferred — but no ctx → visible


def test_is_visible_inside_context_filters_deferred() -> None:
    _reset_discovered()
    with bind_discovered_tools():
        assert is_visible("read_text_file") is True  # core stays visible
        assert is_visible("jira_create_issue") is False  # deferred + undiscovered


def test_mark_discovered_makes_deferred_visible() -> None:
    _reset_discovered()
    with bind_discovered_tools() as discovered:
        assert is_visible("jira_create_issue") is False
        mark_discovered("jira_create_issue")
        assert is_visible("jira_create_issue") is True
        assert "jira_create_issue" in discovered


def test_mark_discovered_outside_context_is_noop() -> None:
    _reset_discovered()
    mark_discovered("jira_create_issue")
    # No exception, and the tool isn't suddenly stored anywhere.
    assert DISCOVERED_TOOLS.get() is None


def test_bind_discovered_tools_isolates_each_block() -> None:
    _reset_discovered()
    with bind_discovered_tools():
        mark_discovered("foo")
    # After exit, nothing is bound and a fresh bind starts empty.
    assert DISCOVERED_TOOLS.get() is None
    with bind_discovered_tools() as second:
        assert second == set()


def test_filter_visible_drops_deferred_keeps_core() -> None:
    _reset_discovered()
    tools = [
        _spec("read_text_file"),  # core — stays
        _spec("jira_create_issue"),  # deferred — drops
        _spec("write_text_file"),  # core — stays
        _spec("postman_run"),  # deferred — drops
    ]
    with bind_discovered_tools():
        visible = filter_visible(tools)
    assert [t.name for t in visible] == ["read_text_file", "write_text_file"]


def test_filter_visible_includes_discovered_deferred() -> None:
    _reset_discovered()
    tools = [
        _spec("read_text_file"),
        _spec("jira_create_issue"),
        _spec("postman_run"),
    ]
    with bind_discovered_tools():
        mark_discovered("jira_create_issue")
        visible = filter_visible(tools)
    assert [t.name for t in visible] == ["read_text_file", "jira_create_issue"]


def test_filter_visible_no_context_returns_all() -> None:
    """When no discovery context is bound (e.g. tests, direct calls), don't filter."""
    _reset_discovered()
    tools = [_spec("read_text_file"), _spec("jira_create_issue")]
    visible = filter_visible(tools)
    assert len(visible) == 2


# ---------------------------------------------------------------------------
# Phase-3 env helpers (is_phase3_active, parse_extra_core_patterns,
# effective_core_names, is_effectively_core)
# ---------------------------------------------------------------------------


def test_is_phase3_active_default_off() -> None:
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("OBSCURA_PHASE3_SDK_TIER", None)
        assert is_phase3_active() is False


def test_is_phase3_active_truthy_values() -> None:
    for v in ("1", "true", "TRUE", "yes", "on"):
        with patch.dict(os.environ, {"OBSCURA_PHASE3_SDK_TIER": v}):
            assert is_phase3_active() is True


def test_is_phase3_active_falsy_values() -> None:
    for v in ("0", "false", "no", "off", ""):
        with patch.dict(os.environ, {"OBSCURA_PHASE3_SDK_TIER": v}):
            assert is_phase3_active() is False


def test_parse_extra_core_patterns_empty() -> None:
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("OBSCURA_PHASE3_EXTRA_CORE", None)
        assert parse_extra_core_patterns() == ()


def test_parse_extra_core_patterns_strips_and_splits() -> None:
    with patch.dict(
        os.environ, {"OBSCURA_PHASE3_EXTRA_CORE": " jira_*, supabase_query ,, foo "}
    ):
        assert parse_extra_core_patterns() == ("jira_*", "supabase_query", "foo")


def test_effective_core_names_with_no_extras_returns_core() -> None:
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("OBSCURA_PHASE3_EXTRA_CORE", None)
        assert effective_core_names() == CORE_TOOL_NAMES


def test_effective_core_names_includes_exact_extras() -> None:
    with patch.dict(
        os.environ, {"OBSCURA_PHASE3_EXTRA_CORE": "jira_create_issue,foo_bar"}
    ):
        result = effective_core_names()
        assert "jira_create_issue" in result
        assert "foo_bar" in result
        # Core still in.
        assert "read_text_file" in result


def test_effective_core_names_expands_globs_with_universe() -> None:
    universe = ["jira_create", "jira_view", "postman_run", "supabase_query"]
    with patch.dict(os.environ, {"OBSCURA_PHASE3_EXTRA_CORE": "jira_*,supabase_query"}):
        result = effective_core_names(universe)
        assert "jira_create" in result
        assert "jira_view" in result
        assert "supabase_query" in result
        assert "postman_run" not in result


def test_effective_core_names_drops_globs_without_universe() -> None:
    """Without all_tool_names, globs can't be expanded — silently dropped."""
    with patch.dict(os.environ, {"OBSCURA_PHASE3_EXTRA_CORE": "jira_*"}):
        result = effective_core_names()
        # CORE only — glob couldn't expand.
        assert result == CORE_TOOL_NAMES


def test_is_effectively_core_for_core_tool() -> None:
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("OBSCURA_PHASE3_EXTRA_CORE", None)
        assert is_effectively_core("read_text_file") is True


def test_is_effectively_core_for_exact_extra() -> None:
    with patch.dict(os.environ, {"OBSCURA_PHASE3_EXTRA_CORE": "jira_create_issue"}):
        assert is_effectively_core("jira_create_issue") is True
        assert is_effectively_core("postman_run") is False


def test_is_effectively_core_for_glob_match() -> None:
    universe = ["jira_create", "jira_view", "postman_run"]
    with patch.dict(os.environ, {"OBSCURA_PHASE3_EXTRA_CORE": "jira_*"}):
        assert is_effectively_core("jira_create", universe) is True
        assert is_effectively_core("jira_view", universe) is True
        assert is_effectively_core("postman_run", universe) is False
