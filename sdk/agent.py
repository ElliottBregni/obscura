"""
sdk.agent — BaseAgent with Analyze → Plan → Execute → Respond (APER) loop.

Provides a reusable agent abstraction. Subclasses override the four phase
methods; each can be deterministic Python or call ``self._client.send()``
for LLM-driven behaviour.

Usage::

    class MyCrawler(BaseAgent):
        async def analyze(self, ctx): ...
        async def plan(self, ctx): ...
        async def execute(self, ctx): ...
        async def respond(self, ctx): ...

    async with ObscuraClient("copilot", ...) as client:
        agent = MyCrawler(client)
        result = await agent.run(input_data)
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable, TYPE_CHECKING

from sdk._types import AgentContext, AgentPhase, HookPoint

if TYPE_CHECKING:
    from sdk.client import ObscuraClient
    from sdk.context import ContextLoader


class BaseAgent:
    """Reusable agent with an APER loop.

    Subclasses override :meth:`analyze`, :meth:`plan`, :meth:`execute`,
    and :meth:`respond`. The :meth:`run` method orchestrates the loop and
    fires hooks at phase boundaries.
    """

    def __init__(
        self,
        client: ObscuraClient,
        *,
        name: str = "agent",
        context_loader: ContextLoader | None = None,
    ) -> None:
        self._client = client
        self._name = name
        self._context_loader = context_loader
        self._hooks: dict[HookPoint, list[Callable]] = {hp: [] for hp in HookPoint}

    # -- Hook registration ---------------------------------------------------

    def on(self, hook: HookPoint, callback: Callable) -> None:
        """Register a lifecycle hook callback."""
        self._hooks[hook].append(callback)

    async def _fire_hook(self, hook: HookPoint, ctx: AgentContext) -> None:
        """Fire all callbacks registered for *hook*."""
        for cb in self._hooks.get(hook, []):
            result = cb(ctx)
            if asyncio.iscoroutine(result):
                await result

    # -- APER lifecycle (override in subclasses) -----------------------------

    async def analyze(self, ctx: AgentContext) -> None:
        """Read input, classify the task, extract context. Sets ``ctx.analysis``."""
        raise NotImplementedError

    async def plan(self, ctx: AgentContext) -> None:
        """Generate a plan of action. Sets ``ctx.plan``."""
        raise NotImplementedError

    async def execute(self, ctx: AgentContext) -> None:
        """Execute the plan. Appends results to ``ctx.results``."""
        raise NotImplementedError

    async def respond(self, ctx: AgentContext) -> None:
        """Format and return the final response. Sets ``ctx.response``."""
        raise NotImplementedError

    # -- Orchestrator --------------------------------------------------------

    async def run(self, input_data: Any = None) -> Any:
        """Execute the full APER loop and return ``ctx.response``."""
        ctx = AgentContext(phase=AgentPhase.ANALYZE, input_data=input_data)

        # Load context from vault if a loader was provided
        if self._context_loader is not None:
            ctx.metadata["system_prompt"] = self._context_loader.load_system_prompt()

        # Get tracer lazily (no-op if OTel not installed)
        _tracer = _get_agent_tracer()

        with _tracer.start_as_current_span(
            f"agent.run.{self._name}",
        ) as run_span:
            _set_span_attr(run_span, "agent.name", self._name)

            # Analyze
            ctx.phase = AgentPhase.ANALYZE
            await self._fire_hook(HookPoint.PRE_ANALYZE, ctx)
            with _tracer.start_as_current_span("agent.analyze") as span:
                _set_span_attr(span, "agent.phase", "analyze")
                await self.analyze(ctx)

            # Plan
            ctx.phase = AgentPhase.PLAN
            with _tracer.start_as_current_span("agent.plan") as span:
                _set_span_attr(span, "agent.phase", "plan")
                await self.plan(ctx)
            await self._fire_hook(HookPoint.POST_PLAN, ctx)

            # Execute
            ctx.phase = AgentPhase.EXECUTE
            await self._fire_hook(HookPoint.PRE_EXECUTE, ctx)
            with _tracer.start_as_current_span("agent.execute") as span:
                _set_span_attr(span, "agent.phase", "execute")
                await self.execute(ctx)

            # Respond
            ctx.phase = AgentPhase.RESPOND
            with _tracer.start_as_current_span("agent.respond") as span:
                _set_span_attr(span, "agent.phase", "respond")
                await self.respond(ctx)
            await self._fire_hook(HookPoint.POST_RESPOND, ctx)

        return ctx.response


# ---------------------------------------------------------------------------
# Lazy telemetry helpers (no-op when OTel is unavailable)
# ---------------------------------------------------------------------------

def _get_agent_tracer() -> Any:
    """Return a tracer, falling back to no-op if telemetry is unavailable."""
    try:
        from sdk.telemetry.traces import get_tracer
        return get_tracer("obscura.agent")
    except Exception:
        return NoOpTracer()


from sdk.telemetry.traces import NoOpTracer


def _set_span_attr(span: Any, key: str, value: Any) -> None:
    """Safely set a span attribute."""
    try:
        if hasattr(span, "set_attribute"):
            span.set_attribute(key, value)
    except Exception:
        pass
