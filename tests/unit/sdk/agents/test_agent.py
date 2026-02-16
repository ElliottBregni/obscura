"""Tests for sdk.agent — BaseAgent APER loop."""

from __future__ import annotations

from unittest.mock import MagicMock
from typing import Any, override

import pytest

from sdk.internal.types import AgentContext, AgentPhase, HookPoint
from sdk.agent.agent import BaseAgent


# ---------------------------------------------------------------------------
# Concrete test agent
# ---------------------------------------------------------------------------


class StubAgent(BaseAgent):
    """Minimal agent for testing the APER loop."""

    def __init__(self, client: MagicMock, **kwargs: Any):
        super().__init__(client, **kwargs)
        self.call_order: list[str] = []

    @override
    async def analyze(self, ctx: AgentContext) -> None:
        self.call_order.append("analyze")
        ctx.analysis = {"items": [1, 2, 3]}

    @override
    async def plan(self, ctx: AgentContext) -> None:
        self.call_order.append("plan")
        ctx.plan = ctx.analysis["items"]

    @override
    async def execute(self, ctx: AgentContext) -> None:
        self.call_order.append("execute")
        ctx.results = [x * 2 for x in ctx.plan]

    @override
    async def respond(self, ctx: AgentContext) -> None:
        self.call_order.append("respond")
        ctx.response = ctx.results


# ---------------------------------------------------------------------------
# APER loop execution order
# ---------------------------------------------------------------------------


class TestAPERLoop:
    @pytest.mark.asyncio
    async def test_phases_execute_in_order(self) -> None:
        client = MagicMock()
        agent = StubAgent(client)
        await agent.run()
        assert agent.call_order == ["analyze", "plan", "execute", "respond"]

    @pytest.mark.asyncio
    async def test_context_flows_through_phases(self) -> None:
        client = MagicMock()
        agent = StubAgent(client)
        result = await agent.run()
        assert result == [2, 4, 6]

    @pytest.mark.asyncio
    async def test_input_data_passed_to_context(self) -> None:
        client = MagicMock()

        class InputAgent(BaseAgent):
            @override
            async def analyze(self, ctx: AgentContext) -> None:
                ctx.analysis = ctx.input_data

            @override
            async def plan(self, ctx: AgentContext) -> None:
                ctx.plan = ctx.analysis

            @override
            async def execute(self, ctx: AgentContext) -> None:
                ctx.results = ctx.plan

            @override
            async def respond(self, ctx: AgentContext) -> None:
                ctx.response = ctx.results

        agent = InputAgent(client)
        result = await agent.run(input_data={"key": "value"})
        assert result == {"key": "value"}


# ---------------------------------------------------------------------------
# Hook firing
# ---------------------------------------------------------------------------


class TestHooks:
    @pytest.mark.asyncio
    async def test_all_hooks_fire_in_order(self) -> None:
        client = MagicMock()
        agent = StubAgent(client)
        fired: list[str] = []

        agent.on(HookPoint.PRE_ANALYZE, lambda ctx: fired.append("pre_analyze"))
        agent.on(HookPoint.POST_ANALYZE, lambda ctx: fired.append("post_analyze"))
        agent.on(HookPoint.PRE_PLAN, lambda ctx: fired.append("pre_plan"))
        agent.on(HookPoint.POST_PLAN, lambda ctx: fired.append("post_plan"))
        agent.on(HookPoint.PRE_EXECUTE, lambda ctx: fired.append("pre_execute"))
        agent.on(HookPoint.POST_EXECUTE, lambda ctx: fired.append("post_execute"))
        agent.on(HookPoint.PRE_RESPOND, lambda ctx: fired.append("pre_respond"))
        agent.on(HookPoint.POST_RESPOND, lambda ctx: fired.append("post_respond"))

        await agent.run()

        assert fired == [
            "pre_analyze", "post_analyze",
            "pre_plan", "post_plan",
            "pre_execute", "post_execute",
            "pre_respond", "post_respond",
        ]

    @pytest.mark.asyncio
    async def test_async_hooks(self) -> None:
        client = MagicMock()
        agent = StubAgent(client)
        fired: list[str] = []

        async def async_hook(ctx: AgentContext) -> None:
            fired.append(f"async_{ctx.phase.value}")

        agent.on(HookPoint.PRE_ANALYZE, async_hook)
        await agent.run()

        assert "async_analyze" in fired

    @pytest.mark.asyncio
    async def test_multiple_hooks_per_point(self) -> None:
        client = MagicMock()
        agent = StubAgent(client)
        fired: list[str] = []

        agent.on(HookPoint.PRE_ANALYZE, lambda ctx: fired.append("first"))
        agent.on(HookPoint.PRE_ANALYZE, lambda ctx: fired.append("second"))

        await agent.run()

        assert fired[:2] == ["first", "second"]

    @pytest.mark.asyncio
    async def test_hook_receives_correct_phase(self) -> None:
        client = MagicMock()
        agent = StubAgent(client)
        phases: list[tuple[str, AgentPhase]] = []

        agent.on(HookPoint.POST_ANALYZE, lambda ctx: phases.append(("post_analyze", ctx.phase)))
        agent.on(HookPoint.PRE_PLAN, lambda ctx: phases.append(("pre_plan", ctx.phase)))
        agent.on(HookPoint.PRE_EXECUTE, lambda ctx: phases.append(("pre_execute", ctx.phase)))
        agent.on(HookPoint.POST_EXECUTE, lambda ctx: phases.append(("post_execute", ctx.phase)))

        await agent.run()

        # POST hooks see the phase they just completed; PRE hooks see the phase about to run
        assert phases == [
            ("post_analyze", AgentPhase.ANALYZE),
            ("pre_plan", AgentPhase.PLAN),
            ("pre_execute", AgentPhase.EXECUTE),
            ("post_execute", AgentPhase.EXECUTE),
        ]


    @pytest.mark.asyncio
    async def test_post_analyze_sees_analysis(self) -> None:
        """POST_ANALYZE fires after analyze() sets ctx.analysis."""
        client = MagicMock()
        agent = StubAgent(client)
        captured: list[Any] = []

        agent.on(HookPoint.POST_ANALYZE, lambda ctx: captured.append(ctx.analysis))

        await agent.run()

        assert captured == [{"items": [1, 2, 3]}]

    @pytest.mark.asyncio
    async def test_post_execute_sees_results(self) -> None:
        """POST_EXECUTE fires after execute() populates ctx.results."""
        client = MagicMock()
        agent = StubAgent(client)
        captured: list[Any] = []

        agent.on(HookPoint.POST_EXECUTE, lambda ctx: captured.append(list(ctx.results)))

        await agent.run()

        assert captured == [[2, 4, 6]]


# ---------------------------------------------------------------------------
# Context loader integration
# ---------------------------------------------------------------------------


class TestContextLoader:
    @pytest.mark.asyncio
    async def test_context_loader_populates_metadata(self) -> None:
        client = MagicMock()
        loader = MagicMock()
        loader.load_system_prompt.return_value = "You are an architect."

        agent = StubAgent(client, context_loader=loader)
        await agent.run()

        loader.load_system_prompt.assert_called_once()


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    @pytest.mark.asyncio
    async def test_not_implemented_raises(self) -> None:
        client = MagicMock()
        agent = BaseAgent(client)
        with pytest.raises(NotImplementedError):
            await agent.run()

    @pytest.mark.asyncio
    async def test_agent_name(self) -> None:
        client = MagicMock()
        agent = StubAgent(client, name="my_agent")
        assert agent.name == "my_agent"
