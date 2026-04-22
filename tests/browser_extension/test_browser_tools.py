"""Unit tests for browser tool specifications.

browser_tools.py is a standalone script loaded via sys.path (not a
regular package module). We mirror the host's import approach: add
the native-host directory to sys.path and import browser_tools directly.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any

import pytest

NATIVE_HOST_DIR = str(
    Path(__file__).parent.parent.parent
    / "packages"
    / "browser-extension"
    / "native-host"
)


def _get_tools() -> list[Any]:
    """Import browser_tools and return TOOLS list."""
    if NATIVE_HOST_DIR not in sys.path:
        sys.path.insert(0, NATIVE_HOST_DIR)
    if "browser_tools" in sys.modules:
        mod = sys.modules["browser_tools"]
    else:
        mod = importlib.import_module("browser_tools")
    return list(getattr(mod, "TOOLS", []))


@pytest.fixture(scope="module")
def tools() -> list[Any]:
    """Browser tool specs loaded from browser_tools.py."""
    return _get_tools()


class TestToolSpecs:
    def test_all_tools_have_names(self, tools: list[Any]) -> None:
        for tool in tools:
            assert tool.name, f"Tool missing name: {tool}"
            assert tool.name.startswith(
                "browser_"
            ), f"Tool name should start with browser_: {tool.name}"

    def test_all_tools_have_handlers(self, tools: list[Any]) -> None:
        for tool in tools:
            assert callable(
                tool.handler
            ), f"Tool {tool.name} has no callable handler"

    def test_all_tools_have_descriptions(self, tools: list[Any]) -> None:
        for tool in tools:
            assert tool.description, f"Tool {tool.name} has no description"

    def test_tool_count(self, tools: list[Any]) -> None:
        assert (
            len(tools) == 9
        ), f"Expected 9 browser tools, got {len(tools)}"

    def test_mutating_tools_marked(self, tools: list[Any]) -> None:
        mutating = {
            "browser_click",
            "browser_fill",
            "browser_eval",
            "browser_switch_tab",
            "browser_navigate",
        }
        for tool in tools:
            if tool.name in mutating:
                assert (
                    tool.side_effects == "mutating"
                ), f"Tool {tool.name} should be marked mutating"

    def test_readonly_tools_not_mutating(self, tools: list[Any]) -> None:
        readonly = {
            "browser_read_page",
            "browser_query_selector",
            "browser_list_tabs",
            "browser_screenshot",
        }
        for tool in tools:
            if tool.name in readonly:
                assert tool.side_effects != "mutating", (
                    f"Tool {tool.name} should NOT be marked mutating"
                )

    def test_tool_names_unique(self, tools: list[Any]) -> None:
        names: list[str] = [t.name for t in tools]
        assert len(names) == len(set(names)), (
            f"Duplicate tool names: {[n for n in names if names.count(n) > 1]}"
        )

    def test_parameters_are_json_schema(self, tools: list[Any]) -> None:
        for tool in tools:
            params: dict[str, Any] = dict(tool.parameters)
            assert params.get("type") == "object", (
                f"Tool {tool.name} parameters root should be type=object"
            )
