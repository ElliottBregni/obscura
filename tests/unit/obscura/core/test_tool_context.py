"""Tests for ToolContext — the per-call session context bound by the agent loop."""

from __future__ import annotations

import asyncio

import pytest

from obscura.core.tool_context import (
    ToolContext,
    bind_tool_context,
    current_tool_context,
)
from obscura.core.tools import ToolRegistry


class TestToolContext:
    def test_unbound_returns_none(self) -> None:
        """When no context is bound, current_tool_context returns None."""
        assert current_tool_context() is None

    def test_bind_and_read(self) -> None:
        reg = ToolRegistry()
        ctx = ToolContext(registry=reg, user="alice")
        with bind_tool_context(ctx):
            current = current_tool_context()
            assert current is ctx
            assert current.registry is reg
            assert current.user == "alice"

    def test_unbinds_after_block(self) -> None:
        ctx = ToolContext(registry=ToolRegistry())
        with bind_tool_context(ctx):
            assert current_tool_context() is ctx
        assert current_tool_context() is None

    def test_unbinds_on_exception(self) -> None:
        ctx = ToolContext(registry=ToolRegistry())
        with pytest.raises(RuntimeError, match="boom"):
            with bind_tool_context(ctx):
                raise RuntimeError("boom")
        assert current_tool_context() is None

    def test_nested_bindings_stack(self) -> None:
        outer = ToolContext(user="outer")
        inner = ToolContext(user="inner")
        with bind_tool_context(outer):
            assert current_tool_context().user == "outer"
            with bind_tool_context(inner):
                assert current_tool_context().user == "inner"
            assert current_tool_context().user == "outer"
        assert current_tool_context() is None

    def test_history_reference_is_mutable(self) -> None:
        """History list bound in ToolContext is the same object — tools can mutate it."""
        history: list[int] = [1, 2, 3, 4, 5]
        ctx = ToolContext(history=history)
        with bind_tool_context(ctx):
            current = current_tool_context()
            assert current.history is history
            del current.history[1:3]
        # Mutation through ToolContext propagated to the original list.
        assert history == [1, 4, 5]

    def test_isolated_per_async_task(self) -> None:
        """ContextVar isolates bindings between concurrent asyncio tasks."""

        async def worker(label: str, results: dict[str, str | None]) -> None:
            ctx = ToolContext(user=label)
            with bind_tool_context(ctx):
                # Yield to let the other worker run
                await asyncio.sleep(0)
                current = current_tool_context()
                results[label] = current.user if current else None

        async def go() -> dict[str, str | None]:
            results: dict[str, str | None] = {}
            await asyncio.gather(
                worker("alice", results),
                worker("bob", results),
            )
            return results

        results = asyncio.run(go())
        # Each task saw its own binding, despite running concurrently.
        assert results == {"alice": "alice", "bob": "bob"}


class TestToolSearchUsesContext:
    """tool_search reads the registry from ToolContext when available."""

    @pytest.mark.asyncio
    async def test_uses_context_registry(self) -> None:
        from obscura.core.tools import tool
        from obscura.tools.system import tool_search

        reg = ToolRegistry()

        @tool("custom_thing", "A custom tool")
        def custom_thing() -> str:
            return ""

        reg.register(custom_thing.spec)

        ctx = ToolContext(registry=reg)
        with bind_tool_context(ctx):
            result = await tool_search("custom")

        assert "custom_thing" in result

    @pytest.mark.asyncio
    async def test_falls_back_to_module_global(self, monkeypatch) -> None:
        """When no context is bound, tool_search uses the legacy module global."""
        from obscura.core.tools import tool
        from obscura.tools import system as system_mod

        reg = ToolRegistry()

        @tool("legacy_thing", "A legacy tool")
        def legacy_thing() -> str:
            return ""

        reg.register(legacy_thing.spec)
        # register() auto-wires the module global when registering tool_search;
        # we need to set it explicitly here since this registry doesn't have
        # tool_search registered.
        monkeypatch.setattr(system_mod, "_tool_registry_ref", reg)

        result = await system_mod.tool_search("legacy")
        assert "legacy_thing" in result


class TestHistorySnipUsesContext:
    """history_snip reads the message history from ToolContext."""

    @pytest.mark.asyncio
    async def test_snips_via_context(self) -> None:
        from obscura.tools.system import history_snip

        history = [{"turn": i, "text": f"msg{i}"} for i in range(5)]
        ctx = ToolContext(history=history)
        with bind_tool_context(ctx):
            result = await history_snip(start_turn=1, end_turn=2)

        assert "removed_turns" in result
        assert len(history) == 3  # original 5 minus 2 removed

    @pytest.mark.asyncio
    async def test_no_history_when_unbound(self) -> None:
        from obscura.tools.system import history_snip

        # Ensure no module-global history either
        result = await history_snip(start_turn=0, end_turn=0)
        assert "no_history" in result
