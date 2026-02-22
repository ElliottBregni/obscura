"""
obscura.agent — BaseAgent with Analyze → Plan → Execute → Respond (APER) loop.

Provides a reusable agent abstraction. Subclasses override the four phase
methods; each can be deterministic Python or call ``self._client.send()``
for LLM-driven behaviour.

Every phase boundary fires a pair of hooks (PRE_* before, POST_* after),
giving callers eight interception points for validation, persistence,
audit, or short-circuit logic.

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
from typing import Any, Awaitable, Callable, TYPE_CHECKING

from obscura.agent.interaction import AttentionPriority
from obscura.core.types import AgentContext, AgentPhase, HookPoint

if TYPE_CHECKING:
    from obscura.agent.interaction import InteractionBus, UserResponse
    from obscura.core.client import ObscuraClient
    from obscura.core.context import ContextLoader


HookCallback = Callable[[AgentContext], Awaitable[Any] | Any]


class BaseAgent:
    """Reusable agent with an APER loop.

    Subclasses override :meth:`analyze`, :meth:`plan`, :meth:`execute`,
    and :meth:`respond`. The :meth:`run` method orchestrates the loop and
    fires hooks at every phase boundary.

    Hook firing order::

        PRE_ANALYZE → analyze() → POST_ANALYZE →
        PRE_PLAN → plan() → POST_PLAN →
        PRE_EXECUTE → execute() → POST_EXECUTE →
        PRE_RESPOND → respond() → POST_RESPOND
    """

    def __init__(
        self,
        client: ObscuraClient,
        *,
        name: str = "agent",
        agent_id: str = "",
        context_loader: ContextLoader | None = None,
        interaction_bus: InteractionBus | None = None,
    ) -> None:
        self._client = client
        self._name = name
        self._agent_id = agent_id or f"agent-{name}"
        self._context_loader = context_loader
        self._interaction_bus = interaction_bus
        self._hooks: dict[HookPoint, list[HookCallback]] = {hp: [] for hp in HookPoint}

    @property
    def name(self) -> str:
        """Read-only agent name for telemetry and observability."""
        return self._name

    @property
    def agent_id(self) -> str:
        """Unique agent identifier."""
        return self._agent_id

    @property
    def interaction_bus(self) -> InteractionBus | None:
        """The interaction bus, if wired."""
        return self._interaction_bus

    # -- Interaction helpers -------------------------------------------------

    async def request_attention(
        self,
        message: str,
        *,
        priority: AttentionPriority = AttentionPriority.NORMAL,
        actions: tuple[str, ...] | list[str] | None = None,
        context: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> UserResponse | None:
        """Ask for user attention via the InteractionBus.

        Returns ``None`` if no bus is wired or if the request fails.
        Subclasses can call this at any point during their lifecycle.
        """
        if self._interaction_bus is None:
            return None
        try:
            return await self._interaction_bus.request_attention(
                agent_id=self._agent_id,
                agent_name=self._name,
                message=message,
                priority=priority,
                actions=actions,
                context=context,
                timeout=timeout,
            )
        except Exception:
            import logging as _logging

            _logging.getLogger(__name__).exception("attention request failed")
            return None

    # -- Hook registration ---------------------------------------------------

    def on(self, hook: HookPoint, callback: HookCallback) -> None:
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
        """Execute the full APER loop and return ``ctx.response``.

        Hook firing order::

            PRE_ANALYZE → analyze() → POST_ANALYZE →
            PRE_PLAN → plan() → POST_PLAN →
            PRE_EXECUTE → execute() → POST_EXECUTE →
            PRE_RESPOND → respond() → POST_RESPOND
        """
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
            await self._fire_hook(HookPoint.POST_ANALYZE, ctx)

            # Plan
            ctx.phase = AgentPhase.PLAN
            await self._fire_hook(HookPoint.PRE_PLAN, ctx)
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
            await self._fire_hook(HookPoint.POST_EXECUTE, ctx)

            # Respond
            ctx.phase = AgentPhase.RESPOND
            await self._fire_hook(HookPoint.PRE_RESPOND, ctx)
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
        from obscura.telemetry.traces import get_tracer

        return get_tracer("obscura.agent")
    except Exception:
        return NoOpTracer()


from obscura.telemetry.traces import NoOpTracer


def _set_span_attr(span: Any, key: str, value: Any) -> None:
    """Safely set a span attribute."""
    try:
        if hasattr(span, "set_attribute"):
            span.set_attribute(key, value)
    except Exception:
        pass
