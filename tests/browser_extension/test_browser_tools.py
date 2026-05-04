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
            assert tool.name.startswith("browser_"), (
                f"Tool name should start with browser_: {tool.name}"
            )

    def test_all_tools_have_handlers(self, tools: list[Any]) -> None:
        for tool in tools:
            assert callable(tool.handler), f"Tool {tool.name} has no callable handler"

    def test_all_tools_have_descriptions(self, tools: list[Any]) -> None:
        for tool in tools:
            assert tool.description, f"Tool {tool.name} has no description"

    def test_tool_count(self, tools: list[Any]) -> None:
        # Event-dispatch tools only — CDP-backed tools live in a separate
        # extension and aren't compiled into this host's TOOLS list.
        assert len(tools) == 17, f"Expected 17 browser tools, got {len(tools)}"

    def test_mutating_tools_marked(self, tools: list[Any]) -> None:
        mutating = {
            "browser_click",
            "browser_fill",
            "browser_eval_js",
            "browser_switch_tab",
            "browser_navigate",
            "browser_scroll_to",
            "browser_new_tab",
            "browser_close_tab",
            "browser_reload_tab",
            "browser_go_back",
            "browser_go_forward",
        }
        for tool in tools:
            if tool.name in mutating:
                assert tool.side_effects == "mutating", (
                    f"Tool {tool.name} should be marked mutating"
                )

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


def _load_module() -> Any:
    if NATIVE_HOST_DIR not in sys.path:
        sys.path.insert(0, NATIVE_HOST_DIR)
    if "browser_tools" in sys.modules:
        return sys.modules["browser_tools"]
    return importlib.import_module("browser_tools")


class TestRpcLifecycle:
    """Exercise init()/_call()/resolve() without a real browser."""

    @pytest.fixture(autouse=True)
    def _reset_module(self) -> None:
        mod = _load_module()
        mod._pending.clear()
        mod._write_frame = None

    @pytest.mark.asyncio
    async def test_call_errors_before_init(self) -> None:
        mod = _load_module()
        with pytest.raises(RuntimeError, match="init"):
            await mod._call("read_page", {})

    @pytest.mark.asyncio
    async def test_call_roundtrip_resolves(self) -> None:
        import asyncio as _asyncio

        mod = _load_module()
        sent: list[dict[str, Any]] = []

        async def writer(frame: dict[str, Any]) -> None:
            sent.append(frame)

        mod.init(writer)

        task = _asyncio.create_task(mod._call("list_tabs", {}, timeout=2.0))
        # Yield to let _call send its frame and register the future.
        await _asyncio.sleep(0)
        assert sent, "expected a browser-tool frame to be queued"
        req = sent[-1]
        assert req["type"] == "browser-tool"
        assert req["op"] == "list_tabs"

        mod.resolve(req["id"], True, [{"id": 1, "title": "Home"}], "")
        result = await task
        assert result == [{"id": 1, "title": "Home"}]

    @pytest.mark.asyncio
    async def test_call_propagates_error(self) -> None:
        import asyncio as _asyncio

        mod = _load_module()
        sent: list[dict[str, Any]] = []

        async def writer(frame: dict[str, Any]) -> None:
            sent.append(frame)

        mod.init(writer)

        task = _asyncio.create_task(mod._call("click", {"selector": "x"}, timeout=2.0))
        await _asyncio.sleep(0)
        mod.resolve(sent[-1]["id"], False, None, "no match")

        with pytest.raises(RuntimeError, match="no match"):
            await task

    @pytest.mark.asyncio
    async def test_call_times_out(self) -> None:
        mod = _load_module()

        async def writer(_frame: dict[str, Any]) -> None:
            return None

        mod.init(writer)

        with pytest.raises(RuntimeError, match="timed out"):
            await mod._call("read_page", {}, timeout=0.05)
        # No leaked pending entries after timeout.
        assert mod._pending == {}

    @pytest.mark.asyncio
    async def test_resolve_ignores_unknown_id(self) -> None:
        mod = _load_module()
        # Should not raise — just a no-op.
        mod.resolve("no-such-id", True, {"ok": True}, "")
