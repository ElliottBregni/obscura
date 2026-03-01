"""APER profile and server-side APER agent for template-based spawning."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import override

from obscura.agent.agent import BaseAgent
from obscura.core.types import AgentContext

# Re-use ObscuraClient via forward reference to avoid circular imports
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from obscura.core.client import ObscuraClient


@dataclass(frozen=True)
class APERProfile:
    """Immutable APER behaviour configuration for each phase."""

    analyze_template: str = "Analyze the user goal and extract constraints."
    plan_template: str = "Create a step-by-step plan to solve the goal."
    execute_template: str = (
        "Goal:\n{goal}\n\nAnalysis:\n{analysis}\n\nPlan:\n{plan}\n\n"
        "Execute using tools where useful and return concise output."
    )
    respond_template: str = "Return a final concise answer based on execution output."
    max_turns: int = 8


class ServerAPERAgent(BaseAgent):
    """APER agent driven by an :class:`APERProfile`.

    Suitable for server-side template spawning where the four APER phases
    are parameterised by the profile's prompt templates.
    """

    def __init__(
        self,
        client: ObscuraClient,
        profile: APERProfile,
        *,
        name: str = "aper-agent",
    ) -> None:
        super().__init__(client, name=name)
        self._profile = profile

    @override
    async def analyze(self, ctx: AgentContext) -> None:
        ctx.analysis = {
            "instruction": self._profile.analyze_template,
            "goal": str(ctx.input_data),
        }

    @override
    async def plan(self, ctx: AgentContext) -> None:
        ctx.plan = {
            "instruction": self._profile.plan_template,
            "steps": [
                "Review goal and context",
                "Use available tools where helpful",
                "Synthesize concise result",
            ],
        }

    @override
    async def execute(self, ctx: AgentContext) -> None:
        prompt = self._profile.execute_template.format(
            goal=str(ctx.input_data),
            analysis=json.dumps(ctx.analysis, indent=2, default=str),
            plan=json.dumps(ctx.plan, indent=2, default=str),
        )
        result = await self._client.run_loop_to_completion(
            prompt,
            max_turns=self._profile.max_turns,
        )
        ctx.results.append(result)

    @override
    async def respond(self, ctx: AgentContext) -> None:
        output = str(ctx.results[-1]) if ctx.results else ""
        ctx.response = (
            f"{self._profile.respond_template}\n\nExecution Output:\n{output}"
        )
