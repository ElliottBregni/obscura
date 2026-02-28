"""Tests for obscura.core.hooks — Event-driven hook registry."""

from __future__ import annotations

import asyncio

import pytest

from obscura.core.hooks import HookRegistry
from obscura.core.types import AgentEvent, AgentEventKind


# ---------------------------------------------------------------------------
# Before-hooks
# ---------------------------------------------------------------------------


class TestBeforeHooks:
    @pytest.mark.asyncio
    async def test_before_hook_receives_event(self) -> None:
        hooks = HookRegistry()
        received: list[AgentEvent] = []

        @hooks.before(AgentEventKind.TOOL_CALL)
        def capture(event: AgentEvent) -> AgentEvent:
            received.append(event)
            return event

        event = AgentEvent(kind=AgentEventKind.TOOL_CALL, tool_name="search")
        result = await hooks.run_before(event)

        assert result is not None
        assert len(received) == 1
        assert received[0].tool_name == "search"

    @pytest.mark.asyncio
    async def test_before_hook_can_modify_event(self) -> None:
        hooks = HookRegistry()

        @hooks.before(AgentEventKind.TEXT_DELTA)
        def add_prefix(event: AgentEvent) -> AgentEvent:
            return AgentEvent(
                kind=event.kind,
                text=f"[modified] {event.text}",
                turn=event.turn,
            )

        event = AgentEvent(kind=AgentEventKind.TEXT_DELTA, text="hello", turn=1)
        result = await hooks.run_before(event)

        assert result is not None
        assert result.text == "[modified] hello"

    @pytest.mark.asyncio
    async def test_before_hook_can_suppress_event(self) -> None:
        hooks = HookRegistry()

        @hooks.before(AgentEventKind.TEXT_DELTA)
        def suppress(_event: AgentEvent) -> None:
            return None

        event = AgentEvent(kind=AgentEventKind.TEXT_DELTA, text="secret")
        result = await hooks.run_before(event)
        assert result is None

    @pytest.mark.asyncio
    async def test_before_hooks_chain_in_order(self) -> None:
        hooks = HookRegistry()
        order: list[str] = []

        @hooks.before(AgentEventKind.TURN_START)
        def first(event: AgentEvent) -> AgentEvent:
            order.append("first")
            return event

        @hooks.before(AgentEventKind.TURN_START)
        def second(event: AgentEvent) -> AgentEvent:
            order.append("second")
            return event

        event = AgentEvent(kind=AgentEventKind.TURN_START, turn=1)
        await hooks.run_before(event)
        assert order == ["first", "second"]

    @pytest.mark.asyncio
    async def test_before_hook_suppression_stops_chain(self) -> None:
        hooks = HookRegistry()
        called: list[str] = []

        @hooks.before(AgentEventKind.TURN_START)
        def blocker(_event: AgentEvent) -> None:
            called.append("blocker")
            return None

        @hooks.before(AgentEventKind.TURN_START)
        def after_blocker(event: AgentEvent) -> AgentEvent:
            called.append("after")
            return event

        event = AgentEvent(kind=AgentEventKind.TURN_START, turn=1)
        result = await hooks.run_before(event)
        assert result is None
        assert called == ["blocker"]

    @pytest.mark.asyncio
    async def test_async_before_hook(self) -> None:
        hooks = HookRegistry()

        @hooks.before(AgentEventKind.TOOL_CALL)
        async def async_hook(event: AgentEvent) -> AgentEvent:
            await asyncio.sleep(0.01)
            return AgentEvent(
                kind=event.kind,
                tool_name=f"async_{event.tool_name}",
                turn=event.turn,
            )

        event = AgentEvent(kind=AgentEventKind.TOOL_CALL, tool_name="read")
        result = await hooks.run_before(event)
        assert result is not None
        assert result.tool_name == "async_read"

    @pytest.mark.asyncio
    async def test_before_hook_wrong_kind_not_called(self) -> None:
        hooks = HookRegistry()
        called = False

        @hooks.before(AgentEventKind.TOOL_CALL)
        def should_not_fire(event: AgentEvent) -> AgentEvent:
            nonlocal called
            called = True
            return event

        event = AgentEvent(kind=AgentEventKind.TEXT_DELTA, text="hi")
        await hooks.run_before(event)
        assert not called


# ---------------------------------------------------------------------------
# After-hooks
# ---------------------------------------------------------------------------


class TestAfterHooks:
    @pytest.mark.asyncio
    async def test_after_hook_fires(self) -> None:
        hooks = HookRegistry()
        received: list[AgentEvent] = []

        @hooks.after(AgentEventKind.AGENT_DONE)
        def capture(event: AgentEvent) -> None:
            received.append(event)

        event = AgentEvent(kind=AgentEventKind.AGENT_DONE, text="done")
        await hooks.run_after(event)
        assert len(received) == 1

    @pytest.mark.asyncio
    async def test_async_after_hook(self) -> None:
        hooks = HookRegistry()
        received: list[str] = []

        @hooks.after(AgentEventKind.TURN_COMPLETE)
        async def async_capture(event: AgentEvent) -> None:
            await asyncio.sleep(0.01)
            received.append(event.text)

        event = AgentEvent(kind=AgentEventKind.TURN_COMPLETE, text="done", turn=1)
        await hooks.run_after(event)
        assert received == ["done"]

    @pytest.mark.asyncio
    async def test_after_hook_exception_logged_not_raised(self) -> None:
        hooks = HookRegistry()

        @hooks.after(AgentEventKind.AGENT_DONE)
        def explode(_event: AgentEvent) -> None:
            raise RuntimeError("boom")

        event = AgentEvent(kind=AgentEventKind.AGENT_DONE, text="done")
        # Should not raise
        await hooks.run_after(event)


# ---------------------------------------------------------------------------
# Wildcards
# ---------------------------------------------------------------------------


class TestWildcardHooks:
    @pytest.mark.asyncio
    async def test_wildcard_before_fires_on_all_kinds(self) -> None:
        hooks = HookRegistry()
        seen_kinds: list[AgentEventKind] = []

        @hooks.before()  # kind=None → wildcard
        def catch_all(event: AgentEvent) -> AgentEvent:
            seen_kinds.append(event.kind)
            return event

        for kind in [
            AgentEventKind.TURN_START,
            AgentEventKind.TEXT_DELTA,
            AgentEventKind.TOOL_CALL,
        ]:
            await hooks.run_before(AgentEvent(kind=kind))

        assert seen_kinds == [
            AgentEventKind.TURN_START,
            AgentEventKind.TEXT_DELTA,
            AgentEventKind.TOOL_CALL,
        ]

    @pytest.mark.asyncio
    async def test_wildcard_runs_before_specific(self) -> None:
        hooks = HookRegistry()
        order: list[str] = []

        @hooks.before()  # wildcard
        def wildcard(event: AgentEvent) -> AgentEvent:
            order.append("wildcard")
            return event

        @hooks.before(AgentEventKind.TURN_START)
        def specific(event: AgentEvent) -> AgentEvent:
            order.append("specific")
            return event

        event = AgentEvent(kind=AgentEventKind.TURN_START, turn=1)
        await hooks.run_before(event)
        assert order == ["wildcard", "specific"]

    @pytest.mark.asyncio
    async def test_wildcard_after_fires_on_all(self) -> None:
        hooks = HookRegistry()
        count = 0

        @hooks.after()
        def count_all(_event: AgentEvent) -> None:
            nonlocal count
            count += 1

        await hooks.run_after(AgentEvent(kind=AgentEventKind.TURN_START))
        await hooks.run_after(AgentEvent(kind=AgentEventKind.AGENT_DONE))
        assert count == 2


# ---------------------------------------------------------------------------
# Imperative registration + clear
# ---------------------------------------------------------------------------


class TestRegistryManagement:
    def test_add_before_imperative(self) -> None:
        hooks = HookRegistry()
        hooks.add_before(lambda e: e, kind=AgentEventKind.TURN_START)
        assert hooks.count == 1

    def test_add_after_imperative(self) -> None:
        hooks = HookRegistry()
        hooks.add_after(lambda e: None, kind=AgentEventKind.AGENT_DONE)
        assert hooks.count == 1

    def test_clear(self) -> None:
        hooks = HookRegistry()
        hooks.add_before(lambda e: e)
        hooks.add_after(lambda e: None)
        assert hooks.count == 2
        hooks.clear()
        assert hooks.count == 0

    @pytest.mark.asyncio
    async def test_before_hook_failure_does_not_suppress(self) -> None:
        """A failing before-hook should not suppress the event."""
        hooks = HookRegistry()

        @hooks.before(AgentEventKind.TEXT_DELTA)
        def explode(_event: AgentEvent) -> AgentEvent:
            raise RuntimeError("hook failed")

        event = AgentEvent(kind=AgentEventKind.TEXT_DELTA, text="keep me")
        result = await hooks.run_before(event)
        # Should return original event, not suppress
        assert result is not None
        assert result.text == "keep me"
