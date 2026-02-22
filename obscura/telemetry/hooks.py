"""
obscura.telemetry.hooks — Auto-instrumentation via HookPoint.

Registers OTel hooks with :class:`~obscura.agent.BaseAgent` instances to
automatically create spans for each APER phase, record tool call metrics,
and measure phase durations.

Usage::

    from obscura.telemetry.hooks import register_telemetry_hooks

    agent = MyAgent(client)
    register_telemetry_hooks(agent)
    result = await agent.run(input_data)
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from obscura.agent.agent import BaseAgent
    from obscura.core.types import AgentContext


def register_telemetry_hooks(agent: BaseAgent) -> None:
    """Register OTel telemetry hooks on *agent* for automatic instrumentation.

    Creates spans for each APER phase and for tool use. Records phase
    durations and tool call counts/durations as metrics.
    """
    from obscura.core.types import HookPoint

    # Shared state for timing phases and tool calls
    _phase_starts: dict[str, float] = {}
    _tool_starts: dict[str, float] = {}
    _context_tokens: dict[str, object] = {}  # for context.detach()

    agent_name = getattr(agent, "name", getattr(agent, "_name", "agent"))

    # -- Phase hooks -----------------------------------------------------------

    def _on_pre_analyze(ctx: AgentContext) -> None:
        _phase_starts["analyze"] = time.monotonic()
        _start_phase_span("analyze", agent_name, _context_tokens)

    def _on_post_plan(ctx: AgentContext) -> None:
        # End analyze span (covers analyze + plan)
        _end_phase_span(
            "analyze", agent_name, _phase_starts.pop("analyze", None), _context_tokens
        )
        _phase_starts["plan"] = time.monotonic()

    def _on_pre_execute(ctx: AgentContext) -> None:
        _end_phase_span(
            "plan", agent_name, _phase_starts.pop("plan", None), _context_tokens
        )
        _phase_starts["execute"] = time.monotonic()
        _start_phase_span("execute", agent_name, _context_tokens)

    def _on_post_respond(ctx: AgentContext) -> None:
        _end_phase_span(
            "execute", agent_name, _phase_starts.pop("execute", None), _context_tokens
        )
        # Record the full run as a metric
        try:
            from obscura.telemetry.metrics import get_metrics

            m = get_metrics()
            m.agent_runs_total.add(1, {"agent_name": agent_name, "status": "success"})
        except ImportError:
            pass

    # -- Tool hooks ------------------------------------------------------------

    def _on_pre_tool_use(ctx: AgentContext) -> None:
        tool_name = getattr(ctx, "tool_name", "") if hasattr(ctx, "tool_name") else ""
        if not tool_name and hasattr(ctx, "metadata"):
            tool_name = ctx.metadata.get("tool_name", "unknown")
        if not tool_name:
            tool_name = "unknown"
        _tool_starts[tool_name] = time.monotonic()
        _start_tool_span(tool_name, _context_tokens)

    def _on_post_tool_use(ctx: AgentContext) -> None:
        tool_name = getattr(ctx, "tool_name", "") if hasattr(ctx, "tool_name") else ""
        if not tool_name and hasattr(ctx, "metadata"):
            tool_name = ctx.metadata.get("tool_name", "unknown")
        if not tool_name:
            tool_name = "unknown"
        start = _tool_starts.pop(tool_name, None)
        _end_tool_span(tool_name, start, _context_tokens)

    # -- Register hooks --------------------------------------------------------

    agent.on(HookPoint.PRE_ANALYZE, _on_pre_analyze)
    agent.on(HookPoint.POST_PLAN, _on_post_plan)
    agent.on(HookPoint.PRE_EXECUTE, _on_pre_execute)
    agent.on(HookPoint.POST_RESPOND, _on_post_respond)
    agent.on(HookPoint.PRE_TOOL_USE, _on_pre_tool_use)
    agent.on(HookPoint.POST_TOOL_USE, _on_post_tool_use)


# ---------------------------------------------------------------------------
# Internal helpers — span lifecycle
# ---------------------------------------------------------------------------


def _start_phase_span(
    phase: str, agent_name: str, tokens: dict[str, object] | None = None
) -> None:
    """Start an OTel span for an agent phase."""
    try:
        from opentelemetry import trace
        from opentelemetry import context

        tracer = trace.get_tracer("obscura.agent")
        span = tracer.start_span(
            f"agent.{phase}",
            attributes={
                "agent.name": agent_name,
                "agent.phase": phase,
            },
        )
        ctx = trace.set_span_in_context(span)
        token = context.attach(ctx)
        if tokens is not None:
            tokens[f"phase.{phase}"] = token
    except ImportError:
        pass


def _end_phase_span(
    phase: str,
    agent_name: str,
    start_time: float | None,
    tokens: dict[str, object] | None = None,
) -> None:
    """End the current phase span and record duration metric."""
    try:
        from opentelemetry import trace, context

        span = trace.get_current_span()
        if span and span.is_recording():
            span.end()
        # Detach context token to prevent leaks
        token = tokens.pop(f"phase.{phase}", None) if tokens else None
        if token is not None:
            context.detach(token)  # type: ignore[arg-type]
    except ImportError:
        pass

    if start_time is not None:
        duration = time.monotonic() - start_time
        try:
            from obscura.telemetry.metrics import get_metrics

            m = get_metrics()
            m.agent_phase_duration_seconds.record(
                duration,
                {"agent_name": agent_name, "phase": phase},
            )
        except ImportError:
            pass


def _start_tool_span(tool_name: str, tokens: dict[str, object] | None = None) -> None:
    """Start an OTel span for a tool call."""
    try:
        from opentelemetry import trace

        tracer = trace.get_tracer("obscura.tools")
        span = tracer.start_span(
            f"tool.{tool_name}",
            attributes={"tool.name": tool_name},
        )
        from opentelemetry import context

        ctx = trace.set_span_in_context(span)
        token = context.attach(ctx)
        if tokens is not None:
            tokens[f"tool.{tool_name}"] = token
    except ImportError:
        pass


def _end_tool_span(
    tool_name: str, start_time: float | None, tokens: dict[str, object] | None = None
) -> None:
    """End the current tool span and record metrics."""
    try:
        from opentelemetry import trace, context

        span = trace.get_current_span()
        if span and span.is_recording():
            span.end()
        # Detach context token to prevent leaks
        token = tokens.pop(f"tool.{tool_name}", None) if tokens else None
        if token is not None:
            context.detach(token)  # type: ignore[arg-type]
    except ImportError:
        pass


# Public wrappers for testing/observability
def start_phase_span(
    phase: str, agent_name: str, tokens: dict[str, object] | None = None
) -> None:
    _start_phase_span(phase, agent_name, tokens)


def end_phase_span(
    phase: str,
    agent_name: str,
    start_time: float | None,
    tokens: dict[str, object] | None = None,
) -> None:
    _end_phase_span(phase, agent_name, start_time, tokens)


def start_tool_span(tool_name: str, tokens: dict[str, object] | None = None) -> None:
    _start_tool_span(tool_name, tokens)


def end_tool_span(
    tool_name: str, start_time: float | None, tokens: dict[str, object] | None = None
) -> None:
    _end_tool_span(tool_name, start_time, tokens)
    status = "success"
    try:
        from obscura.telemetry.metrics import get_metrics

        m = get_metrics()
        m.tool_calls_total.add(1, {"tool_name": tool_name, "status": status})
        if start_time is not None:
            duration = time.monotonic() - start_time
            m.tool_duration_seconds.record(duration, {"tool_name": tool_name})
    except ImportError:
        pass
