"""Comprehensive tests for obscura.plugins.broker.ToolBroker."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from obscura.core.types import (
    ToolCallEnvelope,
    ToolCallContext,
    ToolErrorType,
    ToolExecutionError,
    ToolResultEnvelope,
)
from obscura.plugins.broker import BrokerAuditEntry, ToolBroker, _auto_deny
from obscura.plugins.policy import PolicyAction, PolicyDecision


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_envelope(
    call_id: str = "c1",
    agent_id: str = "agent-1",
    tool: str = "my_tool",
    args: dict | None = None,
) -> ToolCallEnvelope:
    return ToolCallEnvelope(
        call_id=call_id,
        agent_id=agent_id,
        tool=tool,
        args=args or {},
    )


def _allow_decision(rule: str = "allow-all") -> PolicyDecision:
    return PolicyDecision(action=PolicyAction.ALLOW, reason="allowed", matched_rule=rule)


def _deny_decision(reason: str = "forbidden", rule: str = "deny-rule") -> PolicyDecision:
    return PolicyDecision(action=PolicyAction.DENY, reason=reason, matched_rule=rule)


def _approve_decision(reason: str = "needs approval", rule: str = "approve-rule") -> PolicyDecision:
    return PolicyDecision(action=PolicyAction.APPROVE, reason=reason, matched_rule=rule)


def _mock_policy(decision: PolicyDecision | None = None) -> MagicMock:
    policy = MagicMock()
    policy.can_execute_tool.return_value = decision or _allow_decision()
    return policy


def _mock_resolver(tool_names: set[str] | None = None) -> MagicMock:
    resolver = MagicMock()
    resolver.resolve_tool_names.return_value = tool_names or set()
    return resolver


def _sync_handler(**kwargs):
    return {"echo": kwargs}


async def _async_handler(**kwargs):
    return {"echo": kwargs}


# ---------------------------------------------------------------------------
# 1. Constructor defaults and custom params
# ---------------------------------------------------------------------------

class TestConstructor:
    def test_defaults(self):
        policy = _mock_policy()
        broker = ToolBroker(policy_engine=policy)
        assert broker._policy is policy
        assert broker._resolver is None
        assert broker._default_timeout == 30.0
        assert broker._max_retries == 0
        assert broker._handlers == {}
        assert broker._audit_log == []

    def test_custom_params(self):
        policy = _mock_policy()
        resolver = _mock_resolver()
        cb = AsyncMock(return_value=True)
        broker = ToolBroker(
            policy_engine=policy,
            capability_resolver=resolver,
            approval_callback=cb,
            default_timeout=10.0,
            max_retries=3,
        )
        assert broker._resolver is resolver
        assert broker._approval is cb
        assert broker._default_timeout == 10.0
        assert broker._max_retries == 3

    def test_default_approval_callback_is_auto_deny(self):
        broker = ToolBroker(policy_engine=_mock_policy())
        assert broker._approval is _auto_deny


# ---------------------------------------------------------------------------
# 2. register_handler
# ---------------------------------------------------------------------------

class TestRegisterHandler:
    def test_register(self):
        broker = ToolBroker(policy_engine=_mock_policy())
        broker.register_handler("tool_a", _sync_handler)
        assert broker._handlers["tool_a"] is _sync_handler

    def test_overwrite(self):
        broker = ToolBroker(policy_engine=_mock_policy())
        broker.register_handler("tool_a", _sync_handler)
        broker.register_handler("tool_a", _async_handler)
        assert broker._handlers["tool_a"] is _async_handler

    def test_multiple_tools(self):
        broker = ToolBroker(policy_engine=_mock_policy())
        broker.register_handler("tool_a", _sync_handler)
        broker.register_handler("tool_b", _async_handler)
        assert len(broker._handlers) == 2


# ---------------------------------------------------------------------------
# 3. Policy deny
# ---------------------------------------------------------------------------

class TestPolicyDeny:
    async def test_denied_status(self):
        broker = ToolBroker(policy_engine=_mock_policy(_deny_decision()))
        result = await broker.execute(_make_envelope())
        assert result.status == "denied"
        assert result.error is not None
        assert result.error.type == ToolErrorType.UNAUTHORIZED
        assert "forbidden" in result.error.message

    async def test_denied_preserves_call_id_and_tool(self):
        broker = ToolBroker(policy_engine=_mock_policy(_deny_decision()))
        env = _make_envelope(call_id="x99", tool="blocked_tool")
        result = await broker.execute(env)
        assert result.call_id == "x99"
        assert result.tool == "blocked_tool"

    async def test_denied_audit_entry(self):
        broker = ToolBroker(policy_engine=_mock_policy(_deny_decision(rule="rule-7")))
        await broker.execute(_make_envelope())
        assert len(broker.audit_log) == 1
        entry = broker.audit_log[0]
        assert entry.action == "denied"
        assert entry.matched_rule == "rule-7"


# ---------------------------------------------------------------------------
# 4. Capability check deny
# ---------------------------------------------------------------------------

class TestCapabilityDeny:
    async def test_denied_when_tool_not_in_set(self):
        resolver = _mock_resolver({"other_tool"})
        broker = ToolBroker(policy_engine=_mock_policy(), capability_resolver=resolver)
        result = await broker.execute(_make_envelope(tool="my_tool"))
        assert result.status == "denied"
        assert result.error.type == ToolErrorType.UNAUTHORIZED
        assert "capability" in result.error.message.lower()

    async def test_denied_audit_has_capability_check_rule(self):
        resolver = _mock_resolver({"other"})
        broker = ToolBroker(policy_engine=_mock_policy(), capability_resolver=resolver)
        await broker.execute(_make_envelope())
        assert broker.audit_log[0].matched_rule == "capability-check"


# ---------------------------------------------------------------------------
# 5. Capability check passes when no resolver
# ---------------------------------------------------------------------------

class TestCapabilityNoResolver:
    async def test_passes_without_resolver(self):
        broker = ToolBroker(policy_engine=_mock_policy())
        broker.register_handler("my_tool", _sync_handler)
        result = await broker.execute(_make_envelope())
        assert result.status == "ok"

    async def test_passes_when_tool_in_resolved_set(self):
        resolver = _mock_resolver({"my_tool"})
        broker = ToolBroker(policy_engine=_mock_policy(), capability_resolver=resolver)
        broker.register_handler("my_tool", _sync_handler)
        result = await broker.execute(_make_envelope())
        assert result.status == "ok"


# ---------------------------------------------------------------------------
# 6. Approval required -> callback True -> proceeds
# ---------------------------------------------------------------------------

class TestApprovalApproved:
    async def test_execution_proceeds(self):
        cb = AsyncMock(return_value=True)
        broker = ToolBroker(
            policy_engine=_mock_policy(_approve_decision()),
            approval_callback=cb,
        )
        broker.register_handler("my_tool", _sync_handler)
        result = await broker.execute(_make_envelope())
        assert result.status == "ok"
        cb.assert_awaited_once()

    async def test_approval_callback_receives_envelope_and_reason(self):
        cb = AsyncMock(return_value=True)
        broker = ToolBroker(
            policy_engine=_mock_policy(_approve_decision(reason="confirm pls")),
            approval_callback=cb,
        )
        broker.register_handler("my_tool", _sync_handler)
        env = _make_envelope()
        await broker.execute(env)
        cb.assert_awaited_once_with(env, "confirm pls")


# ---------------------------------------------------------------------------
# 7. Approval required -> callback False -> approval_denied
# ---------------------------------------------------------------------------

class TestApprovalDenied:
    async def test_approval_denied_status(self):
        cb = AsyncMock(return_value=False)
        broker = ToolBroker(
            policy_engine=_mock_policy(_approve_decision()),
            approval_callback=cb,
        )
        broker.register_handler("my_tool", _sync_handler)
        result = await broker.execute(_make_envelope())
        assert result.status == "approval_denied"
        assert result.error.type == ToolErrorType.UNAUTHORIZED

    async def test_approval_denied_audit(self):
        cb = AsyncMock(return_value=False)
        broker = ToolBroker(
            policy_engine=_mock_policy(_approve_decision(rule="apr-1")),
            approval_callback=cb,
        )
        broker.register_handler("my_tool", _sync_handler)
        await broker.execute(_make_envelope())
        assert broker.audit_log[0].action == "approval_denied"
        assert broker.audit_log[0].matched_rule == "apr-1"

    async def test_auto_deny_default(self):
        broker = ToolBroker(policy_engine=_mock_policy(_approve_decision()))
        broker.register_handler("my_tool", _sync_handler)
        result = await broker.execute(_make_envelope())
        assert result.status == "approval_denied"


# ---------------------------------------------------------------------------
# 8. Successful sync handler execution
# ---------------------------------------------------------------------------

class TestSyncHandler:
    async def test_sync_returns_ok(self):
        broker = ToolBroker(policy_engine=_mock_policy())
        broker.register_handler("my_tool", _sync_handler)
        result = await broker.execute(_make_envelope(args={"key": "val"}))
        assert result.status == "ok"
        assert result.result == {"echo": {"key": "val"}}

    async def test_sync_error_is_none(self):
        broker = ToolBroker(policy_engine=_mock_policy())
        broker.register_handler("my_tool", _sync_handler)
        result = await broker.execute(_make_envelope())
        assert result.error is None


# ---------------------------------------------------------------------------
# 9. Successful async handler execution
# ---------------------------------------------------------------------------

class TestAsyncHandler:
    async def test_async_returns_ok(self):
        broker = ToolBroker(policy_engine=_mock_policy())
        broker.register_handler("my_tool", _async_handler)
        result = await broker.execute(_make_envelope(args={"a": 1}))
        assert result.status == "ok"
        assert result.result == {"echo": {"a": 1}}

    async def test_async_audit_entry_executed(self):
        broker = ToolBroker(policy_engine=_mock_policy())
        broker.register_handler("my_tool", _async_handler)
        await broker.execute(_make_envelope())
        assert broker.audit_log[0].action == "executed"


# ---------------------------------------------------------------------------
# 10. Handler not found
# ---------------------------------------------------------------------------

class TestNoHandler:
    async def test_no_handler_error(self):
        broker = ToolBroker(policy_engine=_mock_policy())
        result = await broker.execute(_make_envelope(tool="missing"))
        assert result.status == "error"
        assert result.error.type == ToolErrorType.UNKNOWN
        assert "No handler" in result.error.message

    async def test_no_handler_audit(self):
        broker = ToolBroker(policy_engine=_mock_policy())
        await broker.execute(_make_envelope(tool="missing"))
        assert broker.audit_log[0].action == "no_handler"


# ---------------------------------------------------------------------------
# 11. Timeout
# ---------------------------------------------------------------------------

class TestTimeout:
    async def test_timeout_error(self):
        async def slow(**kw):
            await asyncio.sleep(10)

        broker = ToolBroker(policy_engine=_mock_policy(), default_timeout=0.05)
        broker.register_handler("slow", slow)
        result = await broker.execute(_make_envelope(tool="slow"))
        assert result.status == "error"
        assert result.error.type == ToolErrorType.TIMEOUT
        assert result.error.safe_to_retry is True

    async def test_timeout_audit(self):
        async def slow(**kw):
            await asyncio.sleep(10)

        broker = ToolBroker(policy_engine=_mock_policy(), default_timeout=0.05)
        broker.register_handler("slow", slow)
        await broker.execute(_make_envelope(tool="slow"))
        assert broker.audit_log[0].action == "timeout"


# ---------------------------------------------------------------------------
# 12. Handler exception
# ---------------------------------------------------------------------------

class TestHandlerException:
    async def test_exception_returns_error(self):
        async def boom(**kw):
            raise ValueError("kaboom")

        broker = ToolBroker(policy_engine=_mock_policy())
        broker.register_handler("boom", boom)
        result = await broker.execute(_make_envelope(tool="boom"))
        assert result.status == "error"
        assert result.error.type == ToolErrorType.UNKNOWN
        assert result.error.safe_to_retry is False
        assert "kaboom" in result.error.message

    async def test_exception_audit(self):
        async def boom(**kw):
            raise RuntimeError("oops")

        broker = ToolBroker(policy_engine=_mock_policy())
        broker.register_handler("boom", boom)
        await broker.execute(_make_envelope(tool="boom"))
        assert broker.audit_log[0].action == "error"
        assert "oops" in broker.audit_log[0].error

    async def test_sync_exception(self):
        def bad(**kw):
            raise TypeError("bad type")

        broker = ToolBroker(policy_engine=_mock_policy())
        broker.register_handler("bad", bad)
        result = await broker.execute(_make_envelope(tool="bad"))
        assert result.status == "error"
        assert "bad type" in result.error.message


# ---------------------------------------------------------------------------
# 13. Retry logic -- fails then succeeds
# ---------------------------------------------------------------------------

class TestRetrySuccess:
    async def test_retries_then_succeeds(self):
        call_count = 0

        async def flaky(**kw):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise RuntimeError("transient")
            return "ok"

        broker = ToolBroker(policy_engine=_mock_policy(), max_retries=2)
        broker.register_handler("flaky", flaky)
        result = await broker.execute(_make_envelope(tool="flaky"))
        assert result.status == "ok"
        assert result.result == "ok"
        assert call_count == 3

    async def test_retry_audit_is_executed_on_success(self):
        call_count = 0

        async def flaky(**kw):
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise RuntimeError("transient")
            return "ok"

        broker = ToolBroker(policy_engine=_mock_policy(), max_retries=1)
        broker.register_handler("flaky", flaky)
        await broker.execute(_make_envelope(tool="flaky"))
        assert broker.audit_log[0].action == "executed"


# ---------------------------------------------------------------------------
# 14. Retry exhausted
# ---------------------------------------------------------------------------

class TestRetryExhausted:
    async def test_all_attempts_fail(self):
        async def always_fail(**kw):
            raise RuntimeError("nope")

        broker = ToolBroker(policy_engine=_mock_policy(), max_retries=2)
        broker.register_handler("fail", always_fail)
        result = await broker.execute(_make_envelope(tool="fail"))
        assert result.status == "error"
        assert "nope" in result.error.message

    async def test_timeout_retries_exhausted(self):
        async def slow(**kw):
            await asyncio.sleep(10)

        broker = ToolBroker(policy_engine=_mock_policy(), default_timeout=0.05, max_retries=1)
        broker.register_handler("slow", slow)
        result = await broker.execute(_make_envelope(tool="slow"))
        assert result.status == "error"
        assert result.error.type == ToolErrorType.TIMEOUT
        assert result.error.safe_to_retry is True

    async def test_exhausted_audit_entry(self):
        async def always_fail(**kw):
            raise RuntimeError("fail")

        broker = ToolBroker(policy_engine=_mock_policy(), max_retries=1)
        broker.register_handler("fail", always_fail)
        await broker.execute(_make_envelope(tool="fail"))
        assert broker.audit_log[0].action == "error"


# ---------------------------------------------------------------------------
# 15. Audit log entries for each scenario
# ---------------------------------------------------------------------------

class TestAuditLog:
    async def test_audit_log_returns_copy(self):
        broker = ToolBroker(policy_engine=_mock_policy(_deny_decision()))
        await broker.execute(_make_envelope())
        log1 = broker.audit_log
        log2 = broker.audit_log
        assert log1 == log2
        assert log1 is not log2

    async def test_audit_denied_fields(self):
        broker = ToolBroker(policy_engine=_mock_policy(_deny_decision()))
        env = _make_envelope(call_id="d1", agent_id="ag", tool="t")
        await broker.execute(env)
        e = broker.audit_log[0]
        assert e.call_id == "d1"
        assert e.agent_id == "ag"
        assert e.tool == "t"

    async def test_audit_executed_has_latency(self):
        broker = ToolBroker(policy_engine=_mock_policy())
        broker.register_handler("my_tool", _async_handler)
        await broker.execute(_make_envelope())
        assert broker.audit_log[0].latency_ms >= 0

    async def test_audit_timestamp_set(self):
        broker = ToolBroker(policy_engine=_mock_policy(_deny_decision()))
        before = time.time()
        await broker.execute(_make_envelope())
        after = time.time()
        ts = broker.audit_log[0].timestamp
        assert before <= ts <= after

    async def test_multiple_executions_accumulate(self):
        broker = ToolBroker(policy_engine=_mock_policy())
        broker.register_handler("my_tool", _async_handler)
        await broker.execute(_make_envelope(call_id="c1"))
        await broker.execute(_make_envelope(call_id="c2"))
        assert len(broker.audit_log) == 2
        assert broker.audit_log[0].call_id == "c1"
        assert broker.audit_log[1].call_id == "c2"


# ---------------------------------------------------------------------------
# 16. Latency tracking
# ---------------------------------------------------------------------------

class TestLatency:
    async def test_latency_positive_for_executed(self):
        async def slow_ish(**kw):
            await asyncio.sleep(0.01)
            return "done"

        broker = ToolBroker(policy_engine=_mock_policy())
        broker.register_handler("my_tool", slow_ish)
        result = await broker.execute(_make_envelope())
        assert result.latency_ms >= 10

    async def test_latency_on_error_result(self):
        async def boom(**kw):
            raise ValueError("x")

        broker = ToolBroker(policy_engine=_mock_policy())
        broker.register_handler("boom", boom)
        result = await broker.execute(_make_envelope(tool="boom"))
        assert result.latency_ms >= 0

    async def test_latency_on_denied_result(self):
        broker = ToolBroker(policy_engine=_mock_policy(_deny_decision()))
        result = await broker.execute(_make_envelope())
        assert result.latency_ms >= 0


# ---------------------------------------------------------------------------
# 17. Multiple tools registered, each routed correctly
# ---------------------------------------------------------------------------

class TestMultipleToolRouting:
    async def test_routes_to_correct_handler(self):
        async def handler_a(**kw):
            return "A"

        async def handler_b(**kw):
            return "B"

        broker = ToolBroker(policy_engine=_mock_policy())
        broker.register_handler("tool_a", handler_a)
        broker.register_handler("tool_b", handler_b)

        ra = await broker.execute(_make_envelope(tool="tool_a"))
        rb = await broker.execute(_make_envelope(tool="tool_b"))
        assert ra.result == "A"
        assert rb.result == "B"

    async def test_unregistered_among_registered(self):
        broker = ToolBroker(policy_engine=_mock_policy())
        broker.register_handler("tool_a", _sync_handler)
        result = await broker.execute(_make_envelope(tool="tool_x"))
        assert result.status == "error"


# ---------------------------------------------------------------------------
# Extra edge-case tests
# ---------------------------------------------------------------------------

class TestEdgeCases:
    async def test_empty_args(self):
        broker = ToolBroker(policy_engine=_mock_policy())
        broker.register_handler("my_tool", _async_handler)
        result = await broker.execute(_make_envelope(args={}))
        assert result.status == "ok"
        assert result.result == {"echo": {}}

    async def test_handler_returning_none(self):
        async def none_handler(**kw):
            return None

        broker = ToolBroker(policy_engine=_mock_policy())
        broker.register_handler("my_tool", none_handler)
        result = await broker.execute(_make_envelope())
        assert result.status == "ok"
        assert result.result is None

    async def test_policy_checked_before_handler(self):
        """Policy deny should not invoke handler at all."""
        called = False

        async def spy(**kw):
            nonlocal called
            called = True

        broker = ToolBroker(policy_engine=_mock_policy(_deny_decision()))
        broker.register_handler("my_tool", spy)
        await broker.execute(_make_envelope())
        assert called is False

    async def test_capability_checked_before_handler(self):
        called = False

        async def spy(**kw):
            nonlocal called
            called = True

        resolver = _mock_resolver({"other"})
        broker = ToolBroker(policy_engine=_mock_policy(), capability_resolver=resolver)
        broker.register_handler("my_tool", spy)
        await broker.execute(_make_envelope())
        assert called is False

    async def test_approval_denied_does_not_invoke_handler(self):
        called = False

        async def spy(**kw):
            nonlocal called
            called = True

        cb = AsyncMock(return_value=False)
        broker = ToolBroker(
            policy_engine=_mock_policy(_approve_decision()),
            approval_callback=cb,
        )
        broker.register_handler("my_tool", spy)
        await broker.execute(_make_envelope())
        assert called is False

    async def test_call_id_preserved_through_pipeline(self):
        broker = ToolBroker(policy_engine=_mock_policy())
        broker.register_handler("my_tool", _async_handler)
        result = await broker.execute(_make_envelope(call_id="unique-42"))
        assert result.call_id == "unique-42"

    async def test_broker_audit_entry_dataclass(self):
        entry = BrokerAuditEntry(
            call_id="c1", tool="t", agent_id="a", action="executed"
        )
        assert entry.call_id == "c1"
        assert entry.matched_rule == ""
        assert entry.latency_ms == 0
        assert entry.error == ""
        assert entry.timestamp > 0

    async def test_auto_deny_returns_false(self):
        result = await _auto_deny(_make_envelope(), "reason")
        assert result is False

    async def test_max_retries_zero_means_one_attempt(self):
        call_count = 0

        async def counting(**kw):
            nonlocal call_count
            call_count += 1
            raise RuntimeError("fail")

        broker = ToolBroker(policy_engine=_mock_policy(), max_retries=0)
        broker.register_handler("fail", counting)
        await broker.execute(_make_envelope(tool="fail"))
        assert call_count == 1

    async def test_context_default(self):
        env = _make_envelope()
        assert env.context.trace_id == ""
        assert env.context.user_id == ""
        assert env.context.policy == ""

    async def test_handler_with_multiple_kwargs(self):
        broker = ToolBroker(policy_engine=_mock_policy())
        broker.register_handler("my_tool", _async_handler)
        result = await broker.execute(_make_envelope(args={"a": 1, "b": "two", "c": [3]}))
        assert result.result == {"echo": {"a": 1, "b": "two", "c": [3]}}

    async def test_policy_called_with_correct_args(self):
        policy = _mock_policy()
        broker = ToolBroker(policy_engine=policy)
        broker.register_handler("my_tool", _async_handler)
        await broker.execute(_make_envelope(tool="my_tool", agent_id="ag-7"))
        policy.can_execute_tool.assert_called_once_with("my_tool", agent_id="ag-7")

    async def test_resolver_called_with_agent_id(self):
        resolver = _mock_resolver({"my_tool"})
        broker = ToolBroker(policy_engine=_mock_policy(), capability_resolver=resolver)
        broker.register_handler("my_tool", _async_handler)
        await broker.execute(_make_envelope(agent_id="ag-9"))
        resolver.resolve_tool_names.assert_called_once_with("ag-9")
