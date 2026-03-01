"""Reusable test agent subclasses.

Consolidates ``StubAgent`` and factory helpers from test files.

Usage::

    from obscura.testing import StubAgent, make_stub_agent

    agent = make_stub_agent()
    result = await agent.run(input_data={"items": [1, 2, 3]})
"""

from __future__ import annotations

from typing import Any, override
from unittest.mock import MagicMock

from obscura.agent.agent import BaseAgent
from obscura.core.types import AgentContext

__all__ = ["StubAgent", "make_stub_agent"]


class StubAgent(BaseAgent):
    """Minimal APER agent that records call order and passes data through.

    Useful for verifying hook ordering, phase transitions, and context flow.

    The default behaviour:

    * ``analyze`` → sets ``ctx.analysis = ctx.input_data or {"items": [1, 2, 3]}``
    * ``plan``    → sets ``ctx.plan = ctx.analysis["items"]``
    * ``execute`` → sets ``ctx.results = [x * 2 for x in ctx.plan]``
    * ``respond`` → sets ``ctx.response = ctx.results``
    """

    def __init__(self, client: Any = None, **kwargs: Any) -> None:
        super().__init__(client or MagicMock(), **kwargs)
        self.call_order: list[str] = []

    @override
    async def analyze(self, ctx: AgentContext) -> None:
        self.call_order.append("analyze")
        ctx.analysis = ctx.input_data if ctx.input_data is not None else {"items": [1, 2, 3]}

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


def make_stub_agent(**kwargs: Any) -> StubAgent:
    """Factory that creates a :class:`StubAgent` with a mock client.

    All keyword arguments are forwarded to :class:`StubAgent.__init__`.
    """
    return StubAgent(**kwargs)
