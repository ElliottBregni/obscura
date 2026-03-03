"""Tests for obscura.agent.aper — APERProfile and ServerAPERAgent."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from obscura.agent.aper import APERProfile, ServerAPERAgent
from obscura.core.types import AgentContext, AgentPhase


class TestAPERProfile:
    def test_defaults(self) -> None:
        p = APERProfile()
        assert "Analyze" in p.analyze_template
        assert "{goal}" in p.execute_template
        assert p.max_turns == 8

    def test_frozen(self) -> None:
        p = APERProfile()
        with pytest.raises(AttributeError):
            p.max_turns = 10  # type: ignore[misc]

    def test_custom(self) -> None:
        p = APERProfile(analyze_template="custom", max_turns=3)
        assert p.analyze_template == "custom"
        assert p.max_turns == 3


class TestServerAPERAgent:
    def _make_mock_client(self) -> MagicMock:
        client = MagicMock()
        client.run_loop_to_completion = AsyncMock(return_value="exec result")
        client.on = MagicMock()
        # Provide a backend_impl mock for BaseAgent
        client.backend_impl = MagicMock()
        client.backend_impl.register_hook = MagicMock()
        return client

    @pytest.mark.asyncio
    async def test_analyze(self) -> None:
        client = self._make_mock_client()
        agent = ServerAPERAgent(client, APERProfile(), name="test")
        ctx = AgentContext(phase=AgentPhase.ANALYZE, input_data="find stocks")
        await agent.analyze(ctx)
        assert ctx.analysis is not None
        assert ctx.analysis["goal"] == "find stocks"

    @pytest.mark.asyncio
    async def test_plan(self) -> None:
        client = self._make_mock_client()
        agent = ServerAPERAgent(client, APERProfile(), name="test")
        ctx = AgentContext(phase=AgentPhase.PLAN, input_data="find stocks")
        await agent.plan(ctx)
        assert ctx.plan is not None
        assert "steps" in ctx.plan

    @pytest.mark.asyncio
    async def test_execute(self) -> None:
        client = self._make_mock_client()
        profile = APERProfile(max_turns=3)
        agent = ServerAPERAgent(client, profile, name="test")
        ctx = AgentContext(
            phase=AgentPhase.EXECUTE,
            input_data="find stocks",
            analysis={"goal": "find stocks"},
            plan={"steps": ["search"]},
        )
        await agent.execute(ctx)
        assert len(ctx.results) == 1
        assert ctx.results[0] == "exec result"
        client.run_loop_to_completion.assert_awaited_once()
        # Verify max_turns from profile
        call_kwargs: dict[str, Any] = client.run_loop_to_completion.call_args.kwargs
        assert call_kwargs["max_turns"] == 3

    @pytest.mark.asyncio
    async def test_respond(self) -> None:
        client = self._make_mock_client()
        agent = ServerAPERAgent(client, APERProfile(), name="test")
        ctx = AgentContext(
            phase=AgentPhase.RESPOND,
            input_data="find stocks",
            results=["some output"],
        )
        await agent.respond(ctx)
        assert ctx.response is not None
        assert "some output" in ctx.response

    @pytest.mark.asyncio
    async def test_respond_empty_results(self) -> None:
        client = self._make_mock_client()
        agent = ServerAPERAgent(client, APERProfile(), name="test")
        ctx = AgentContext(phase=AgentPhase.RESPOND, input_data="x")
        await agent.respond(ctx)
        assert ctx.response is not None
