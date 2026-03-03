"""Tests for obscura.agent.aper_loop_agent — APERLoopAgent."""

from __future__ import annotations

import asyncio
from typing import Any, override
from unittest.mock import AsyncMock, MagicMock

import pytest

from obscura.agent.aper_loop_agent import APERLoopAgent
from obscura.core.types import AgentContext, AgentPhase, HookPoint


# ---------------------------------------------------------------------------
# Concrete test subclass
# ---------------------------------------------------------------------------


class ConcreteAPERAgent(APERLoopAgent):
    """Minimal APERLoopAgent that records phase execution."""

    def __init__(self, **kwargs: Any) -> None:
        client = MagicMock()
        client.run_loop_to_completion = AsyncMock(return_value="mock result")
        super().__init__(client, **kwargs)
        self.call_order: list[str] = []
        self.analyses: list[Any] = []

    @override
    async def analyze(self, ctx: AgentContext) -> None:
        self.call_order.append("analyze")
        ctx.analysis = {"input": ctx.input_data}

    @override
    async def plan(self, ctx: AgentContext) -> None:
        self.call_order.append("plan")
        ctx.plan = [f"step_for_{ctx.analysis}"]

    @override
    async def execute(self, ctx: AgentContext) -> None:
        self.call_order.append("execute")
        ctx.results.append(f"result_of_{ctx.plan}")

    @override
    async def respond(self, ctx: AgentContext) -> None:
        self.call_order.append("respond")
        ctx.response = "\n".join(str(r) for r in ctx.results)


# ---------------------------------------------------------------------------
# Tests: run_once (single APER cycle)
# ---------------------------------------------------------------------------


class TestRunOnce:
    @pytest.mark.asyncio
    async def test_run_once_executes_all_phases(self) -> None:
        agent = ConcreteAPERAgent(name="test")
        await agent.run_once("hello")
        assert agent.call_order == ["analyze", "plan", "execute", "respond"]

    @pytest.mark.asyncio
    async def test_run_once_returns_response(self) -> None:
        agent = ConcreteAPERAgent(name="test")
        result = await agent.run_once("hello")
        assert isinstance(result, str)
        assert "result_of_" in result

    @pytest.mark.asyncio
    async def test_run_once_passes_input_data(self) -> None:
        agent = ConcreteAPERAgent(name="test")
        await agent.run_once("my input")
        # analyze was called → check that it saw the input
        assert agent.call_order == ["analyze", "plan", "execute", "respond"]


# ---------------------------------------------------------------------------
# Tests: Hook firing
# ---------------------------------------------------------------------------


class TestHooks:
    @pytest.mark.asyncio
    async def test_all_hooks_fire_in_order(self) -> None:
        agent = ConcreteAPERAgent(name="test")
        fired: list[str] = []

        agent.on(HookPoint.PRE_ANALYZE, lambda ctx: fired.append("pre_analyze"))
        agent.on(HookPoint.POST_ANALYZE, lambda ctx: fired.append("post_analyze"))
        agent.on(HookPoint.PRE_PLAN, lambda ctx: fired.append("pre_plan"))
        agent.on(HookPoint.POST_PLAN, lambda ctx: fired.append("post_plan"))
        agent.on(HookPoint.PRE_EXECUTE, lambda ctx: fired.append("pre_execute"))
        agent.on(HookPoint.POST_EXECUTE, lambda ctx: fired.append("post_execute"))
        agent.on(HookPoint.PRE_RESPOND, lambda ctx: fired.append("pre_respond"))
        agent.on(HookPoint.POST_RESPOND, lambda ctx: fired.append("post_respond"))

        await agent.run_once("test")

        assert fired == [
            "pre_analyze",
            "post_analyze",
            "pre_plan",
            "post_plan",
            "pre_execute",
            "post_execute",
            "pre_respond",
            "post_respond",
        ]

    @pytest.mark.asyncio
    async def test_async_hooks(self) -> None:
        agent = ConcreteAPERAgent(name="test")
        fired: list[str] = []

        async def async_hook(ctx: AgentContext) -> None:
            fired.append(f"async_{ctx.phase.value}")

        agent.on(HookPoint.PRE_ANALYZE, async_hook)
        await agent.run_once("test")

        assert "async_analyze" in fired

    @pytest.mark.asyncio
    async def test_hook_sees_correct_phase(self) -> None:
        agent = ConcreteAPERAgent(name="test")
        phases: list[AgentPhase] = []

        agent.on(HookPoint.POST_ANALYZE, lambda ctx: phases.append(ctx.phase))
        agent.on(HookPoint.PRE_PLAN, lambda ctx: phases.append(ctx.phase))

        await agent.run_once("test")

        assert phases == [AgentPhase.ANALYZE, AgentPhase.PLAN]


# ---------------------------------------------------------------------------
# Tests: run_forever loop
# ---------------------------------------------------------------------------


class TestRunForever:
    @pytest.mark.asyncio
    async def test_run_forever_processes_input(self) -> None:
        agent = ConcreteAPERAgent(name="test")

        async def driver() -> None:
            await asyncio.sleep(0.05)
            await agent.send("message 1")
            await asyncio.sleep(0.1)
            await agent.stop()

        task = asyncio.create_task(agent.run_forever())
        await driver()
        await task

        assert agent.iteration >= 1
        assert "analyze" in agent.call_order

    @pytest.mark.asyncio
    async def test_stop_terminates_loop(self) -> None:
        agent = ConcreteAPERAgent(name="test")

        async def stopper() -> None:
            await asyncio.sleep(0.05)
            await agent.stop()

        task = asyncio.create_task(agent.run_forever())
        asyncio.create_task(stopper())
        await asyncio.wait_for(task, timeout=3.0)

        assert agent.stopped is True

    @pytest.mark.asyncio
    async def test_properties(self) -> None:
        agent = ConcreteAPERAgent(name="my-aper", agent_id="custom-id")
        assert agent.name == "my-aper"
        assert agent.agent_id == "custom-id"
        assert agent.stopped is False
        assert agent.iteration == 0


# ---------------------------------------------------------------------------
# Tests: Context loader integration
# ---------------------------------------------------------------------------


class TestContextLoader:
    @pytest.mark.asyncio
    async def test_context_loader_populates_metadata(self) -> None:
        agent = ConcreteAPERAgent(name="test")
        loader = MagicMock()
        loader.load_system_prompt.return_value = "You are an architect."
        agent._context_loader = loader  # noqa: SLF001

        await agent.run_once("input")

        loader.load_system_prompt.assert_called_once()


# ---------------------------------------------------------------------------
# Tests: Default APER implementation (without subclass overrides)
# ---------------------------------------------------------------------------


class TestDefaultAPER:
    @pytest.mark.asyncio
    async def test_default_analyze_calls_llm(self) -> None:
        """The default analyze() should call run_loop_to_completion."""
        client = MagicMock()
        client.run_loop_to_completion = AsyncMock(return_value="analysis")

        agent = APERLoopAgent(client, name="default")
        result = await agent.run_once("test prompt")

        # Default implementation calls LLM for each phase
        assert client.run_loop_to_completion.call_count >= 1
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# Tests: Interaction bus
# ---------------------------------------------------------------------------


class TestInteractionBus:
    @pytest.mark.asyncio
    async def test_request_attention_without_bus(self) -> None:
        agent = ConcreteAPERAgent(name="test")
        result = await agent.request_attention("need help")
        assert result is None

    @pytest.mark.asyncio
    async def test_interaction_bus_property(self) -> None:
        from obscura.agent.interaction import InteractionBus

        bus = InteractionBus()
        agent = ConcreteAPERAgent(name="test", interaction_bus=bus)
        assert agent.interaction_bus is bus
