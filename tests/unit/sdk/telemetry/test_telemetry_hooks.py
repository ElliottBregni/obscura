"""Tests for sdk.telemetry.hooks — register_telemetry_hooks and helpers."""

from unittest.mock import MagicMock, patch

from sdk.telemetry.hooks import (
    register_telemetry_hooks,
    start_phase_span,
    end_phase_span,
    start_tool_span,
    end_tool_span,
)
from sdk.internal.types import HookPoint


class TestRegisterTelemetryHooks:
    def test_registers_all_hooks(self):
        agent = MagicMock()
        agent._name = "test-agent"
        register_telemetry_hooks(agent)

        # Should register 6 hooks total
        assert agent.on.call_count == 6
        hook_points = [call[0][0] for call in agent.on.call_args_list]
        assert HookPoint.PRE_ANALYZE in hook_points
        assert HookPoint.POST_PLAN in hook_points
        assert HookPoint.PRE_EXECUTE in hook_points
        assert HookPoint.POST_RESPOND in hook_points
        assert HookPoint.PRE_TOOL_USE in hook_points
        assert HookPoint.POST_TOOL_USE in hook_points

    def test_hooks_callable(self):
        agent = MagicMock()
        agent._name = "test-agent"
        register_telemetry_hooks(agent)

        # All registered callbacks should be callable
        for call in agent.on.call_args_list:
            callback = call[0][1]
            assert callable(callback)


class TestPhaseSpanHelpers:
    def test_start_phase_span_no_otel(self):
        with patch.dict("sys.modules", {"opentelemetry": None}):
            start_phase_span("analyze", "agent1")  # Should not raise

    def test_end_phase_span_no_otel(self):
        with patch.dict("sys.modules", {"opentelemetry": None}):
            end_phase_span("analyze", "agent1", 100.0)  # Should not raise

    def test_end_phase_span_no_start_time(self):
        end_phase_span("analyze", "agent1", None)  # Should not raise

    def test_end_phase_span_with_tokens(self):
        tokens: dict[str, object] = {"phase.test": "fake_token"}
        with patch.dict("sys.modules", {"opentelemetry": None}):
            end_phase_span("test", "agent1", 100.0, tokens)


class TestToolSpanHelpers:
    def test_start_tool_span_no_otel(self):
        with patch.dict("sys.modules", {"opentelemetry": None}):
            start_tool_span("my_tool")  # Should not raise

    def test_end_tool_span_no_otel(self):
        with patch.dict("sys.modules", {"opentelemetry": None}):
            end_tool_span("my_tool", 100.0)  # Should not raise

    def test_end_tool_span_no_start_time(self):
        end_tool_span("my_tool", None)

    def test_end_tool_span_with_tokens(self):
        tokens: dict[str, object] = {"tool.test": "fake_token"}
        with patch.dict("sys.modules", {"opentelemetry": None}):
            end_tool_span("test", 100.0, tokens)


class TestHookCallbacks:
    def test_pre_analyze_callback(self):
        agent = MagicMock()
        agent._name = "agent1"
        register_telemetry_hooks(agent)

        # Get the pre_analyze callback
        pre_analyze = None
        for call in agent.on.call_args_list:
            if call[0][0] == HookPoint.PRE_ANALYZE:
                pre_analyze = call[0][1]
                break

        ctx = MagicMock()
        assert pre_analyze is not None
        pre_analyze(ctx)  # Should not raise

    def test_pre_tool_use_with_tool_name(self):
        agent = MagicMock()
        agent._name = "agent1"
        register_telemetry_hooks(agent)

        pre_tool = None
        for call in agent.on.call_args_list:
            if call[0][0] == HookPoint.PRE_TOOL_USE:
                pre_tool = call[0][1]
                break

        ctx = MagicMock()
        ctx.tool_name = "read_file"
        assert pre_tool is not None
        pre_tool(ctx)  # Should not raise

    def test_pre_tool_use_with_metadata(self):
        agent = MagicMock()
        agent._name = "agent1"
        register_telemetry_hooks(agent)

        pre_tool = None
        for call in agent.on.call_args_list:
            if call[0][0] == HookPoint.PRE_TOOL_USE:
                pre_tool = call[0][1]
                break

        ctx = MagicMock(spec=[])
        ctx.metadata = {"tool_name": "write_file"}
        assert pre_tool is not None
        pre_tool(ctx)  # Should not raise

    def test_post_respond_callback(self):
        agent = MagicMock()
        agent._name = "agent1"
        register_telemetry_hooks(agent)

        post_respond = None
        for call in agent.on.call_args_list:
            if call[0][0] == HookPoint.POST_RESPOND:
                post_respond = call[0][1]
                break

        ctx = MagicMock()
        assert post_respond is not None
        post_respond(ctx)  # Should not raise
