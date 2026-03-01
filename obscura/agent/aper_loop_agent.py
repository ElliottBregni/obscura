"""APERLoopAgent — long-running agent that runs APER per input.

Combines the structured APER lifecycle of :class:`BaseAgent` with the
long-running persistence of :class:`LoopAgent`.  Each incoming message
triggers a full **Analyze → Plan → Execute → Respond** cycle, with
hooks, context loading, and telemetry — but the agent stays alive
between inputs waiting for the next message.

This is the right choice when you want:

* Structured reasoning (APER phases) for every input
* Long-lived agent that persists between interactions
* Hook-based observability across the lifecycle
* User attention requests at any phase

Usage::

    class ResearchAgent(APERLoopAgent):
        async def analyze(self, ctx):
            ctx.analysis = await self._client.send(f"Analyze: {ctx.input_data}")

        async def plan(self, ctx):
            ctx.plan = ["step1", "step2"]

        async def execute(self, ctx):
            for step in ctx.plan:
                result = await self._client.run_loop_to_completion(step)
                ctx.results.append(result)

        async def respond(self, ctx):
            ctx.response = "\\n".join(ctx.results)

    agent = ResearchAgent(client, name="researcher")
    await agent.send("Research quantum computing trends")
    await agent.run_forever()

Config YAML (for supervisor)::

    agents:
      - name: researcher
        type: aper
        model: claude
        system_prompt: "You are a research assistant."
        max_turns: 25
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, TYPE_CHECKING
from uuid import uuid4

from obscura.agent.interaction import (
    AgentInput,
    AgentOutput,
    AttentionPriority,
    InteractionBus,
    UserResponse,
)
from obscura.core.types import AgentContext, AgentEventKind, AgentPhase, HookPoint

if TYPE_CHECKING:
    from obscura.core.client import ObscuraClient
    from obscura.core.context import ContextLoader

__all__ = ["APERLoopAgent"]

logger = logging.getLogger(__name__)

HookCallback = Callable[[AgentContext], Awaitable[Any] | Any]


class APERLoopAgent:
    """Long-running agent that executes APER for each input.

    Architecture::

        ┌──────────── run_forever() ────────────┐
        │  while not stopped:                    │
        │    input = await queue.get()           │
        │    ctx = AgentContext(input_data=input) │
        │    PRE_ANALYZE → analyze(ctx)          │
        │    PRE_PLAN    → plan(ctx)             │
        │    PRE_EXECUTE → execute(ctx)          │
        │    PRE_RESPOND → respond(ctx)          │
        │    emit ctx.response                   │
        └────────────────────────────────────────┘

    Subclasses override ``analyze``, ``plan``, ``execute``, ``respond``
    just like :class:`BaseAgent`.  The difference is this agent stays
    alive between APER cycles, waiting for new inputs.
    """

    def __init__(
        self,
        client: ObscuraClient,
        *,
        name: str = "aper-agent",
        agent_id: str = "",
        context_loader: ContextLoader | None = None,
        interaction_bus: InteractionBus | None = None,
        max_turns_per_input: int = 25,
    ) -> None:
        self._client = client
        self._name = name
        self._agent_id = agent_id or f"aper-{uuid4().hex[:8]}"
        self._context_loader = context_loader
        self._bus = interaction_bus
        self._max_turns = max_turns_per_input
        self._input_queue: asyncio.Queue[AgentInput] = asyncio.Queue()
        self._stopped = False
        self._iteration = 0
        self._hooks: dict[HookPoint, list[HookCallback]] = {hp: [] for hp in HookPoint}

    # -- Public properties ---------------------------------------------------

    @property
    def name(self) -> str:
        return self._name

    @property
    def agent_id(self) -> str:
        return self._agent_id

    @property
    def stopped(self) -> bool:
        return self._stopped

    @property
    def iteration(self) -> int:
        """Number of APER cycles completed."""
        return self._iteration

    @property
    def interaction_bus(self) -> InteractionBus | None:
        return self._bus

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
        """Read input, classify the task, extract context.

        Default: uses the LLM to analyze the input.
        """
        if ctx.input_data:
            result = await self._client.run_loop_to_completion(
                f"Analyze this request and identify key requirements:\n\n{ctx.input_data}",
                max_turns=self._max_turns,
            )
            ctx.analysis = result

    async def plan(self, ctx: AgentContext) -> None:
        """Generate a plan of action.

        Default: uses the LLM to create a plan from the analysis.
        """
        if ctx.analysis:
            result = await self._client.run_loop_to_completion(
                f"Based on this analysis, create an action plan:\n\n{ctx.analysis}",
                max_turns=self._max_turns,
            )
            ctx.plan = result

    async def execute(self, ctx: AgentContext) -> None:
        """Execute the plan.

        Default: uses the LLM tool loop to execute the plan.
        """
        if ctx.plan:
            result = await self._client.run_loop_to_completion(
                f"Execute this plan:\n\n{ctx.plan}",
                max_turns=self._max_turns,
            )
            ctx.results.append(result)

    async def respond(self, ctx: AgentContext) -> None:
        """Format and return the final response.

        Default: joins results into a response string.
        """
        if ctx.results:
            ctx.response = "\n\n".join(str(r) for r in ctx.results)
        else:
            ctx.response = "No results."

    # -- Public API ----------------------------------------------------------

    async def send(self, content: str, *, source: str = "user") -> None:
        """Enqueue a new input for the agent to process via APER."""
        await self._input_queue.put(
            AgentInput(content=content, source=source),
        )

    async def run_forever(self) -> None:
        """Main loop: wait for input → APER cycle → emit response → repeat."""
        logger.info("[%s] APER loop agent started (id=%s)", self._name, self._agent_id)
        self._stopped = False

        try:
            while not self._stopped:
                input_msg = await self._get_next_input()
                if input_msg is None:
                    continue

                logger.debug(
                    "[%s] APER cycle #%d for input from %s",
                    self._name,
                    self._iteration,
                    input_msg.source,
                )

                try:
                    response = await self._run_aper(input_msg.content)
                    if response:
                        await self._emit_output(str(response), is_final=True)
                except Exception:
                    logger.exception("[%s] error during APER cycle", self._name)
                    await self._emit_output(
                        "[error] An error occurred during processing.",
                        is_final=True,
                        event_kind=AgentEventKind.ERROR,
                    )

                self._iteration += 1

        except asyncio.CancelledError:
            logger.info("[%s] APER loop agent cancelled", self._name)
        finally:
            self._stopped = True
            logger.info("[%s] APER loop agent stopped", self._name)

    async def run_once(self, input_data: Any = None) -> Any:
        """Run a single APER cycle synchronously and return the response.

        Useful for one-shot execution or testing without the run_forever loop.
        """
        return await self._run_aper(input_data)

    async def stop(self) -> None:
        """Signal the agent to stop after the current APER cycle."""
        self._stopped = True
        try:
            self._input_queue.put_nowait(
                AgentInput(content="", source="__stop__"),
            )
        except asyncio.QueueFull:
            pass

    # -- APER orchestrator ---------------------------------------------------

    async def _run_aper(self, input_data: Any = None) -> Any:
        """Execute one full APER cycle."""
        ctx = AgentContext(phase=AgentPhase.ANALYZE, input_data=input_data)

        # Load context from vault if a loader was provided
        if self._context_loader is not None:
            ctx.metadata["system_prompt"] = self._context_loader.load_system_prompt()

        # Analyze
        ctx.phase = AgentPhase.ANALYZE
        await self._fire_hook(HookPoint.PRE_ANALYZE, ctx)
        await self.analyze(ctx)
        await self._fire_hook(HookPoint.POST_ANALYZE, ctx)

        # Plan
        ctx.phase = AgentPhase.PLAN
        await self._fire_hook(HookPoint.PRE_PLAN, ctx)
        await self.plan(ctx)
        await self._fire_hook(HookPoint.POST_PLAN, ctx)

        # Execute
        ctx.phase = AgentPhase.EXECUTE
        await self._fire_hook(HookPoint.PRE_EXECUTE, ctx)
        await self.execute(ctx)
        await self._fire_hook(HookPoint.POST_EXECUTE, ctx)

        # Respond
        ctx.phase = AgentPhase.RESPOND
        await self._fire_hook(HookPoint.PRE_RESPOND, ctx)
        await self.respond(ctx)
        await self._fire_hook(HookPoint.POST_RESPOND, ctx)

        return ctx.response

    # -- Internal helpers ----------------------------------------------------

    async def _get_next_input(self) -> AgentInput | None:
        """Block until an input arrives, or return ``None`` if stopped."""
        while not self._stopped:
            try:
                msg = await asyncio.wait_for(
                    self._input_queue.get(), timeout=1.0,
                )
                if msg.source == "__stop__":
                    return None
                return msg
            except asyncio.TimeoutError:
                continue
        return None

    async def _emit_output(
        self,
        text: str,
        *,
        is_final: bool = False,
        event_kind: AgentEventKind | None = None,
    ) -> None:
        """Push output through the InteractionBus (if wired)."""
        if self._bus is None:
            return
        output = AgentOutput(
            agent_id=self._agent_id,
            agent_name=self._name,
            text=text,
            event_kind=event_kind,
            is_final=is_final,
        )
        try:
            await self._bus.emit_output(output)
        except Exception:
            logger.exception("[%s] failed to emit output", self._name)

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

        Can be called at any point during APER phases.
        """
        if self._bus is None:
            return None
        try:
            return await self._bus.request_attention(
                agent_id=self._agent_id,
                agent_name=self._name,
                message=message,
                priority=priority,
                actions=actions,
                context=context,
                timeout=timeout,
            )
        except Exception:
            logger.exception("[%s] attention request failed", self._name)
            return None
