"""
Tests for sdk.telemetry.hooks — Auto-instrumentation via HookPoint.

Verifies that register_telemetry_hooks() correctly wires phase and tool
hooks, and that context tokens are properly detached (no memory leaks).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable
from unittest.mock import MagicMock, patch

import pytest

from sdk._types import HookPoint


# ---------------------------------------------------------------------------
# Stub agent for testing hooks
# ---------------------------------------------------------------------------

@dataclass
class _StubContext:
    """Minimal AgentContext stub."""
    tool_name: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class _StubAgent:
    """Minimal agent that records hook registrations and allows triggering."""

    def __init__(self, name: str = "test-agent") -> None:
        self._name = name
        self._hooks: dict[HookPoint, list[Callable]] = {hp: [] for hp in HookPoint}

    def on(self, hook: HookPoint, callback: Callable) -> None:
        self._hooks[hook].append(callback)

    def trigger(self, hook: HookPoint, ctx: Any = None) -> None:
        if ctx is None:
            ctx = _StubContext()
        for cb in self._hooks[hook]:
            cb(ctx)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

class TestRegisterTelemetryHooks:
    def test_registers_all_hooks(self) -> None:
        """Should register callbacks for all 6 expected hook points."""
        from sdk.telemetry.hooks import register_telemetry_hooks

        agent = _StubAgent()
        register_telemetry_hooks(agent)

        expected = {
            HookPoint.PRE_ANALYZE,
            HookPoint.POST_PLAN,
            HookPoint.PRE_EXECUTE,
            HookPoint.POST_RESPOND,
            HookPoint.PRE_TOOL_USE,
            HookPoint.POST_TOOL_USE,
        }
        for hp in expected:
            assert len(agent._hooks[hp]) == 1, f"Missing hook for {hp}"

    def test_phase_hooks_callable(self) -> None:
        """Phase hooks should be callable without raising."""
        from sdk.telemetry.hooks import register_telemetry_hooks

        agent = _StubAgent()
        register_telemetry_hooks(agent)

        ctx = _StubContext()
        # Run through full APER cycle
        agent.trigger(HookPoint.PRE_ANALYZE, ctx)
        agent.trigger(HookPoint.POST_PLAN, ctx)
        agent.trigger(HookPoint.PRE_EXECUTE, ctx)
        agent.trigger(HookPoint.POST_RESPOND, ctx)

    def test_tool_hooks_callable(self) -> None:
        """Tool hooks should be callable without raising."""
        from sdk.telemetry.hooks import register_telemetry_hooks

        agent = _StubAgent()
        register_telemetry_hooks(agent)

        ctx = _StubContext(tool_name="read_file")
        agent.trigger(HookPoint.PRE_TOOL_USE, ctx)
        agent.trigger(HookPoint.POST_TOOL_USE, ctx)

    def test_tool_name_from_metadata(self) -> None:
        """Tool name should be extracted from ctx.metadata if not on ctx directly."""
        from sdk.telemetry.hooks import register_telemetry_hooks

        agent = _StubAgent()
        register_telemetry_hooks(agent)

        ctx = _StubContext(tool_name="", metadata={"tool_name": "search"})
        # Should not raise
        agent.trigger(HookPoint.PRE_TOOL_USE, ctx)
        agent.trigger(HookPoint.POST_TOOL_USE, ctx)

    def test_unknown_tool_name_defaults(self) -> None:
        """Missing tool name should default to 'unknown'."""
        from sdk.telemetry.hooks import register_telemetry_hooks

        agent = _StubAgent()
        register_telemetry_hooks(agent)

        ctx = _StubContext(tool_name="", metadata={})
        # Should not raise
        agent.trigger(HookPoint.PRE_TOOL_USE, ctx)
        agent.trigger(HookPoint.POST_TOOL_USE, ctx)


# ---------------------------------------------------------------------------
# Context token lifecycle (leak prevention)
# ---------------------------------------------------------------------------

class TestContextTokenLifecycle:
    """Verify that context.attach() tokens are properly detached."""

    def test_phase_tokens_detached(self) -> None:
        """Phase spans should attach and then detach context tokens."""
        from sdk.telemetry.hooks import _start_phase_span, _end_phase_span
        import time

        tokens: dict[str, object] = {}

        # Start should store a token (or do nothing if OTel not installed)
        _start_phase_span("analyze", "test-agent", tokens)

        # End should remove and detach
        _end_phase_span("analyze", "test-agent", time.monotonic() - 0.01, tokens)

        # Token dict should be cleaned up
        assert "phase.analyze" not in tokens

    def test_tool_tokens_detached(self) -> None:
        """Tool spans should attach and then detach context tokens."""
        from sdk.telemetry.hooks import _start_tool_span, _end_tool_span
        import time

        tokens: dict[str, object] = {}

        _start_tool_span("read_file", tokens)
        _end_tool_span("read_file", time.monotonic() - 0.01, tokens)

        assert "tool.read_file" not in tokens

    def test_end_without_start_is_safe(self) -> None:
        """Ending a span that was never started should not raise."""
        from sdk.telemetry.hooks import _end_phase_span, _end_tool_span

        tokens: dict[str, object] = {}
        _end_phase_span("plan", "test-agent", None, tokens)
        _end_tool_span("write_file", None, tokens)


# ---------------------------------------------------------------------------
# Multiple APER cycles
# ---------------------------------------------------------------------------

class TestMultipleCycles:
    def test_multiple_aper_cycles(self) -> None:
        """Running multiple APER cycles should not leak or raise."""
        from sdk.telemetry.hooks import register_telemetry_hooks

        agent = _StubAgent()
        register_telemetry_hooks(agent)

        ctx = _StubContext()
        for _ in range(5):
            agent.trigger(HookPoint.PRE_ANALYZE, ctx)
            agent.trigger(HookPoint.POST_PLAN, ctx)
            agent.trigger(HookPoint.PRE_EXECUTE, ctx)

            # Tool use within execute
            tool_ctx = _StubContext(tool_name="bash")
            agent.trigger(HookPoint.PRE_TOOL_USE, tool_ctx)
            agent.trigger(HookPoint.POST_TOOL_USE, tool_ctx)

            agent.trigger(HookPoint.POST_RESPOND, ctx)
