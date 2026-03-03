"""Tests for declarative hook configuration (from_hook_definitions + merge)."""

from __future__ import annotations

import pytest

from obscura.core.hooks import HookRegistry
from obscura.core.types import AgentEvent, AgentEventKind
from obscura.manifest.models import HookDefinition


class TestFromHookDefinitions:
    def test_pre_tool_use_registered(self) -> None:
        defs = [HookDefinition(event="preToolUse", bash="echo pre")]
        registry = HookRegistry.from_hook_definitions(defs)
        assert registry.count == 1

    def test_post_tool_use_registered(self) -> None:
        defs = [HookDefinition(event="postToolUse", bash="echo post")]
        registry = HookRegistry.from_hook_definitions(defs)
        assert registry.count == 1

    def test_mixed_hooks(self) -> None:
        defs = [
            HookDefinition(event="preToolUse", bash="echo pre"),
            HookDefinition(event="postToolUse", bash="echo post"),
            HookDefinition(event="sessionEnd", bash="echo end"),
        ]
        registry = HookRegistry.from_hook_definitions(defs)
        assert registry.count == 3

    def test_empty_definitions(self) -> None:
        registry = HookRegistry.from_hook_definitions([])
        assert registry.count == 0

    @pytest.mark.asyncio
    async def test_before_hook_passes_through(self) -> None:
        """A before-hook with no bash command passes the event through."""
        defs = [HookDefinition(event="preToolUse", bash="")]
        registry = HookRegistry.from_hook_definitions(defs)
        event = AgentEvent(kind=AgentEventKind.TOOL_CALL, tool_name="test", turn=1)
        result = await registry.run_before(event)
        assert result is not None
        assert result.kind == AgentEventKind.TOOL_CALL


class TestMerge:
    def test_merge_adds_hooks(self) -> None:
        r1 = HookRegistry()
        r2 = HookRegistry()

        @r1.before(AgentEventKind.TOOL_CALL)
        async def h1(e: AgentEvent) -> AgentEvent:
            return e

        @r2.after(AgentEventKind.TOOL_RESULT)
        def h2(e: AgentEvent) -> None:
            pass

        assert r1.count == 1
        r1.merge(r2)
        assert r1.count == 2

    @pytest.mark.asyncio
    async def test_merge_preserves_order(self) -> None:
        order: list[str] = []
        r1 = HookRegistry()
        r2 = HookRegistry()

        @r1.after(AgentEventKind.TOOL_CALL)
        def system_hook(e: AgentEvent) -> None:
            order.append("system")

        @r2.after(AgentEventKind.TOOL_CALL)
        def agent_hook(e: AgentEvent) -> None:
            order.append("agent")

        r1.merge(r2)
        event = AgentEvent(kind=AgentEventKind.TOOL_CALL, tool_name="t", turn=1)
        await r1.run_after(event)
        assert order == ["system", "agent"]
