"""Tests for sdk.agent — BaseAgent APER loop."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from sdk._types import AgentContext, AgentPhase, HookPoint
from sdk.agent import BaseAgent


# ---------------------------------------------------------------------------
# Concrete test agent
# ---------------------------------------------------------------------------

class StubAgent(BaseAgent):
    """Minimal agent for testing the APER loop."""

    def __init__(self, client: MagicMock, **kwargs):
        super().__init__(client, **kwargs)
        self.call_order: list[str] = []

    async def analyze(self, ctx: AgentContext) -> None:
        self.call_order.append("analyze")
        ctx.analysis = {"items": [1, 2, 3]}

    async def plan(self, ctx: AgentContext) -> None:
        self.call_order.append("plan")
        ctx.plan = ctx.analysis["items"]

    async def execute(self, ctx: AgentContext) -> None:
        self.call_order.append("execute")
        ctx.results = [x * 2 for x in ctx.plan]

    async def respond(self, ctx: AgentContext) -> None:
        self.call_order.append("respond")
        ctx.response = ctx.results


# ---------------------------------------------------------------------------
# APER loop execution order
# ---------------------------------------------------------------------------

class TestAPERLoop:
    @pytest.mark.asyncio
    async def test_phases_execute_in_order(self):
        client = MagicMock()
        agent = StubAgent(client)
        result = await agent.run()
        assert agent.call_order == ["analyze", "plan", "execute", "respond"]

    @pytest.mark.asyncio
    async def test_context_flows_through_phases(self):
        client = MagicMock()
        agent = StubAgent(client)
        result = await agent.run()
        assert result == [2, 4, 6]

    @pytest.mark.asyncio
    async def test_input_data_passed_to_context(self):
        client = MagicMock()

        class InputAgent(BaseAgent):
            async def analyze(self, ctx):
                ctx.analysis = ctx.input_data
            async def plan(self, ctx):
                ctx.plan = ctx.analysis
            async def execute(self, ctx):
                ctx.results = ctx.plan
            async def respond(self, ctx):
                ctx.response = ctx.results

        agent = InputAgent(client)
        result = await agent.run(input_data={"key": "value"})
        assert result == {"key": "value"}


# ---------------------------------------------------------------------------
# Hook firing
# ---------------------------------------------------------------------------

class TestHooks:
    @pytest.mark.asyncio
    async def test_hooks_fire_at_boundaries(self):
        client = MagicMock()
        agent = StubAgent(client)
        fired: list[str] = []

        agent.on(HookPoint.PRE_ANALYZE, lambda ctx: fired.append("pre_analyze"))
        agent.on(HookPoint.POST_PLAN, lambda ctx: fired.append("post_plan"))
        agent.on(HookPoint.PRE_EXECUTE, lambda ctx: fired.append("pre_execute"))
        agent.on(HookPoint.POST_RESPOND, lambda ctx: fired.append("post_respond"))

        await agent.run()

        assert fired == ["pre_analyze", "post_plan", "pre_execute", "post_respond"]

    @pytest.mark.asyncio
    async def test_async_hooks(self):
        client = MagicMock()
        agent = StubAgent(client)
        fired: list[str] = []

        async def async_hook(ctx):
            fired.append(f"async_{ctx.phase.value}")

        agent.on(HookPoint.PRE_ANALYZE, async_hook)
        await agent.run()

        assert "async_analyze" in fired

    @pytest.mark.asyncio
    async def test_multiple_hooks_per_point(self):
        client = MagicMock()
        agent = StubAgent(client)
        fired: list[str] = []

        agent.on(HookPoint.PRE_ANALYZE, lambda ctx: fired.append("first"))
        agent.on(HookPoint.PRE_ANALYZE, lambda ctx: fired.append("second"))

        await agent.run()

        assert fired[:2] == ["first", "second"]

    @pytest.mark.asyncio
    async def test_hook_receives_correct_phase(self):
        client = MagicMock()
        agent = StubAgent(client)
        phases: list[AgentPhase] = []

        agent.on(HookPoint.PRE_EXECUTE, lambda ctx: phases.append(ctx.phase))

        await agent.run()

        # PRE_EXECUTE fires after plan sets the phase, before execute runs
        assert phases == [AgentPhase.EXECUTE]


# ---------------------------------------------------------------------------
# Context loader integration
# ---------------------------------------------------------------------------

class TestContextLoader:
    @pytest.mark.asyncio
    async def test_context_loader_populates_metadata(self):
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
    async def test_not_implemented_raises(self):
        client = MagicMock()
        agent = BaseAgent(client)
        with pytest.raises(NotImplementedError):
            await agent.run()

    @pytest.mark.asyncio
    async def test_agent_name(self):
        client = MagicMock()
        agent = StubAgent(client, name="my_agent")
        assert agent._name == "my_agent"
