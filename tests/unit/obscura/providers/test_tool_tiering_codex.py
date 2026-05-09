"""Tests for Codex _build_tool_listing() tool tiering (Changes 1 & 2).

Change 1: Non-core tools appear as deferred lines; core tools get full description.
Change 2: is_shadow specs are excluded from the listing entirely.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from obscura.core.tool_tiering import CORE_TOOL_NAMES
from obscura.core.types import ToolSpec


def _stub_handler(*_a: Any, **_kw: Any) -> str:
    return ""


def _spec(name: str, description: str = "", is_shadow: bool = False) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=description or f"Description for {name}",
        parameters={"type": "object", "properties": {}},
        handler=_stub_handler,
        is_shadow=is_shadow,
    )


def _make_backend_with_tools(tools: list[ToolSpec]) -> Any:
    """Construct a minimal CodexBackend with tools pre-loaded (no SDK needed)."""
    from obscura.core.auth import AuthConfig
    from obscura.providers.codex import CodexBackend

    backend = CodexBackend(AuthConfig(openai_api_key=None))
    backend._tools = tools
    return backend


# ---------------------------------------------------------------------------
# Change 1: tiered listing
# ---------------------------------------------------------------------------


class TestBuildToolListingTiering:
    def test_core_tools_appear_with_full_description(self) -> None:
        """Core tools must appear as full backtick-name-backtick: desc lines."""
        core_name = next(iter(CORE_TOOL_NAMES))
        tools = [_spec(core_name, "Core tool description")]
        backend = _make_backend_with_tools(tools)
        listing = backend._build_tool_listing()

        assert f"`{core_name}`" in listing
        assert "Core tool description" in listing

    def test_non_core_tools_appear_as_deferred(self) -> None:
        """10 non-core tools must be listed in the Discoverable section."""
        non_core = [_spec(f"niche_plugin_{i}") for i in range(10)]
        backend = _make_backend_with_tools(non_core)
        listing = backend._build_tool_listing()

        # None of the non-core tool names should appear in the core section
        # (before "## Discoverable Tools").
        discoverable_idx = listing.find("## Discoverable Tools")
        assert discoverable_idx != -1, "Discoverable Tools section is missing"

        core_section = listing[:discoverable_idx]
        for spec in non_core:
            # Tool names must NOT appear in the core section with full descriptions.
            assert spec.name not in core_section, (
                f"Non-core tool {spec.name!r} appeared in the core section"
            )

        # But they MUST appear in the deferred section.
        deferred_section = listing[discoverable_idx:]
        for spec in non_core:
            assert spec.name in deferred_section, (
                f"Non-core tool {spec.name!r} not found in deferred section"
            )

    def test_mixed_tools_split_correctly(self) -> None:
        """Mixed core + non-core tools: each goes to its correct section."""
        core_name = "read_text_file"  # always in CORE_TOOL_NAMES
        non_core_name = "external_slack_post"
        tools = [_spec(core_name, "Read a file"), _spec(non_core_name, "Post to Slack")]
        backend = _make_backend_with_tools(tools)
        listing = backend._build_tool_listing()

        discoverable_idx = listing.find("## Discoverable Tools")
        assert discoverable_idx != -1

        core_section = listing[:discoverable_idx]
        deferred_section = listing[discoverable_idx:]

        assert core_name in core_section
        assert non_core_name not in core_section
        assert non_core_name in deferred_section

    def test_deferred_line_references_tool_search(self) -> None:
        """The deferred section must mention tool_search so the model knows how to proceed."""
        non_core = [_spec("fancy_analytics_tool")]
        backend = _make_backend_with_tools(non_core)
        listing = backend._build_tool_listing()

        assert "tool_search" in listing


# ---------------------------------------------------------------------------
# Change 2: is_shadow filtering
# ---------------------------------------------------------------------------


class TestShadowToolExclusion:
    def test_shadow_specs_absent_from_listing(self) -> None:
        """Specs with is_shadow=True must not appear in _build_tool_listing()."""
        shadow = _spec("mcp__obs__some_tool", is_shadow=True)
        backend = _make_backend_with_tools([shadow])
        listing = backend._build_tool_listing()

        assert "mcp__obs__some_tool" not in listing

    def test_shadow_specs_still_in_tool_registry(self) -> None:
        """Shadow specs excluded from the listing must remain in _tools for dispatch."""
        shadow = _spec("mcp__obs__shadow_tool", is_shadow=True)
        backend = _make_backend_with_tools([shadow])

        # The spec is registered in _tools but filtered in the listing.
        assert any(s.name == "mcp__obs__shadow_tool" for s in backend._tools)
        listing = backend._build_tool_listing()
        assert "mcp__obs__shadow_tool" not in listing

    def test_non_shadow_specs_appear_in_listing(self) -> None:
        """Non-shadow specs must still appear normally."""
        visible = _spec("read_text_file", is_shadow=False)
        backend = _make_backend_with_tools([visible])
        listing = backend._build_tool_listing()

        assert "read_text_file" in listing

    def test_shadow_flag_default_is_false_on_toolspec(self) -> None:
        """ToolSpec.is_shadow must default to False."""
        spec = _spec("any_tool")
        assert spec.is_shadow is False

    def test_shadow_flag_set_true_on_toolspec(self) -> None:
        """ToolSpec.is_shadow=True must be stored correctly."""
        spec = _spec("mcp__obs__tool", is_shadow=True)
        assert spec.is_shadow is True
