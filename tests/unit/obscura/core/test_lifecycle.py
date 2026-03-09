"""Tests for obscura.core.lifecycle — built-in hook factories."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from obscura.core.hooks import HookRegistry
from obscura.core.lifecycle import (
    make_audit_hook,
    make_memory_inject_hook,
    make_policy_gate_hook,
    make_preflight_hook,
    make_redact_hook,
)
from obscura.core.preflight import PreflightResult, PreflightCheck
from obscura.core.types import AgentEvent, AgentEventKind
from obscura.plugins.broker import BrokerAuditEntry
from obscura.plugins.policy import PolicyAction, PolicyDecision


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tool_call_event(name: str = "run_shell") -> AgentEvent:
    return AgentEvent(kind=AgentEventKind.TOOL_CALL, tool_name=name, turn=1)


def _tool_result_event(result: str = "output", name: str = "run_shell") -> AgentEvent:
    return AgentEvent(
        kind=AgentEventKind.TOOL_RESULT,
        tool_name=name,
        tool_result=result,
        turn=1,
    )


def _turn_start_event(text: str = "") -> AgentEvent:
    return AgentEvent(kind=AgentEventKind.TURN_START, text=text, turn=1)


def _agent_start_event() -> AgentEvent:
    return AgentEvent(kind=AgentEventKind.AGENT_START, turn=0)


def _allow_engine() -> MagicMock:
    engine = MagicMock()
    engine.can_execute_tool.return_value = PolicyDecision(
        action=PolicyAction.ALLOW, reason="ok"
    )
    return engine


def _deny_engine(reason: str = "forbidden") -> MagicMock:
    engine = MagicMock()
    engine.can_execute_tool.return_value = PolicyDecision(
        action=PolicyAction.DENY, reason=reason, matched_rule="test-deny"
    )
    return engine


# ---------------------------------------------------------------------------
# Policy gate hook
# ---------------------------------------------------------------------------


class TestPolicyGateHook:
    def test_allows_on_allow_decision(self) -> None:
        hook = make_policy_gate_hook(_allow_engine())
        event = _tool_call_event()
        result = hook(event)
        assert result is event

    def test_suppresses_on_deny_decision(self) -> None:
        hook = make_policy_gate_hook(_deny_engine())
        event = _tool_call_event()
        result = hook(event)
        assert result is None

    def test_passes_non_tool_call_events(self) -> None:
        hook = make_policy_gate_hook(_deny_engine())
        event = _turn_start_event()
        result = hook(event)
        assert result is event

    def test_checks_correct_tool_name(self) -> None:
        engine = _allow_engine()
        hook = make_policy_gate_hook(engine)
        hook(_tool_call_event("my_special_tool"))
        engine.can_execute_tool.assert_called_once_with("my_special_tool")


# ---------------------------------------------------------------------------
# Audit hook
# ---------------------------------------------------------------------------


class TestAuditHook:
    def test_records_entry_for_tool_call(self) -> None:
        store: list[BrokerAuditEntry] = []
        hook = make_audit_hook(store)
        hook(_tool_call_event())
        assert len(store) == 1
        assert store[0].action == "tool_call"

    def test_records_entry_for_every_event(self) -> None:
        store: list[BrokerAuditEntry] = []
        hook = make_audit_hook(store)
        hook(_tool_call_event())
        hook(_tool_result_event())
        hook(_turn_start_event())
        assert len(store) == 3

    def test_uses_provided_store(self) -> None:
        my_store: list[BrokerAuditEntry] = []
        hook = make_audit_hook(my_store)
        hook(_tool_call_event())
        assert len(my_store) == 1

    def test_creates_default_store(self) -> None:
        hook = make_audit_hook()
        hook(_tool_call_event())
        assert len(hook.store) == 1  # type: ignore[attr-defined]

    def test_entry_has_tool_name(self) -> None:
        store: list[BrokerAuditEntry] = []
        hook = make_audit_hook(store)
        hook(_tool_call_event("my_tool"))
        assert store[0].tool == "my_tool"

    def test_entry_has_timestamp(self) -> None:
        store: list[BrokerAuditEntry] = []
        hook = make_audit_hook(store)
        hook(_tool_call_event())
        assert store[0].timestamp > 0


# ---------------------------------------------------------------------------
# Redact hook
# ---------------------------------------------------------------------------


class TestRedactHook:
    def test_redacts_matching_pattern(self) -> None:
        hook = make_redact_hook([r"sk-[a-zA-Z0-9]+"])
        event = _tool_result_event("key is sk-abc123XYZ")
        hook(event)
        assert "sk-abc123XYZ" not in event.tool_result
        assert "[REDACTED]" in event.tool_result

    def test_no_redaction_without_match(self) -> None:
        hook = make_redact_hook([r"sk-[a-zA-Z0-9]+"])
        event = _tool_result_event("no secrets here")
        hook(event)
        assert event.tool_result == "no secrets here"

    def test_multiple_patterns(self) -> None:
        hook = make_redact_hook([r"sk-\w+", r"ghp_\w+"])
        event = _tool_result_event("keys: sk-abc123 and ghp_tokenXYZ")
        hook(event)
        assert "sk-abc123" not in event.tool_result
        assert "ghp_tokenXYZ" not in event.tool_result
        assert event.tool_result.count("[REDACTED]") == 2

    def test_only_runs_on_tool_result(self) -> None:
        hook = make_redact_hook([r"secret"])
        event = _tool_call_event()
        event.tool_result = "secret data"
        hook(event)
        # Should not redact since it's a TOOL_CALL, not TOOL_RESULT
        assert event.tool_result == "secret data"

    def test_empty_result_no_error(self) -> None:
        hook = make_redact_hook([r"pattern"])
        event = _tool_result_event("")
        hook(event)
        assert event.tool_result == ""


# ---------------------------------------------------------------------------
# Preflight hook
# ---------------------------------------------------------------------------


class TestPreflightHook:
    def test_passes_on_valid_preflight(self) -> None:
        validator = MagicMock()
        validator.validate.return_value = PreflightResult(
            agent_name="ag",
            checks=(PreflightCheck(name="ok", passed=True),),
        )
        hook = make_preflight_hook(validator)
        event = _agent_start_event()
        event._agent = MagicMock()  # type: ignore[attr-defined]
        result = hook(event)
        assert result is event

    def test_suppresses_on_failed_preflight(self) -> None:
        validator = MagicMock()
        validator.validate.return_value = PreflightResult(
            agent_name="ag",
            checks=(PreflightCheck(name="fail", passed=False, message="missing"),),
        )
        hook = make_preflight_hook(validator)
        event = _agent_start_event()
        event._agent = MagicMock()  # type: ignore[attr-defined]
        result = hook(event)
        assert result is None

    def test_passes_non_agent_start_events(self) -> None:
        validator = MagicMock()
        hook = make_preflight_hook(validator)
        event = _tool_call_event()
        result = hook(event)
        assert result is event
        validator.validate.assert_not_called()

    def test_passes_when_no_agent_attached(self) -> None:
        validator = MagicMock()
        hook = make_preflight_hook(validator)
        event = _agent_start_event()
        result = hook(event)
        assert result is event
        validator.validate.assert_not_called()


# ---------------------------------------------------------------------------
# Memory inject hook
# ---------------------------------------------------------------------------


class TestMemoryInjectHook:
    def test_injects_memory_into_turn_start(self) -> None:
        hook = make_memory_inject_hook(lambda: "Remember: user prefers dark mode")
        event = _turn_start_event("original prompt")
        result = hook(event)
        assert result is event
        assert "Remember: user prefers dark mode" in event.text
        assert "original prompt" in event.text

    def test_no_op_on_non_turn_start(self) -> None:
        hook = make_memory_inject_hook(lambda: "memory")
        event = _tool_call_event()
        result = hook(event)
        assert result is event

    def test_handles_empty_existing_text(self) -> None:
        hook = make_memory_inject_hook(lambda: "context")
        event = _turn_start_event("")
        hook(event)
        assert event.text == "context"

    def test_handles_loader_returning_empty(self) -> None:
        hook = make_memory_inject_hook(lambda: "")
        event = _turn_start_event("original")
        hook(event)
        assert event.text == "original"

    def test_handles_loader_exception(self) -> None:
        def bad_loader() -> str:
            raise RuntimeError("memory failed")

        hook = make_memory_inject_hook(bad_loader)
        event = _turn_start_event("original")
        result = hook(event)
        assert result is event
        assert event.text == "original"


# ---------------------------------------------------------------------------
# Hook registration integration
# ---------------------------------------------------------------------------


class TestHookRegistration:
    def test_policy_hook_registered_correctly(self) -> None:
        hooks = HookRegistry()
        hook = make_policy_gate_hook(_allow_engine())
        hooks.add_before(hook, AgentEventKind.TOOL_CALL)
        assert hooks.count == 1

    @pytest.mark.asyncio
    async def test_hooks_fire_in_order(self) -> None:
        order: list[str] = []

        def first(event: AgentEvent) -> None:
            order.append("first")

        def second(event: AgentEvent) -> None:
            order.append("second")

        hooks = HookRegistry()
        hooks.add_after(first, AgentEventKind.TOOL_CALL)
        hooks.add_after(second, AgentEventKind.TOOL_CALL)
        await hooks.run_after(_tool_call_event())
        assert order == ["first", "second"]

    def test_new_event_kinds_exist(self) -> None:
        assert AgentEventKind.AGENT_START.value == "agent_start"
        assert AgentEventKind.AGENT_STOP.value == "agent_stop"
        assert AgentEventKind.PREFLIGHT_PASS.value == "preflight_pass"
        assert AgentEventKind.PREFLIGHT_FAIL.value == "preflight_fail"

    @pytest.mark.asyncio
    async def test_agent_start_hook_fires(self) -> None:
        fired = []

        def on_start(event: AgentEvent) -> None:
            fired.append(event.kind)

        hooks = HookRegistry()
        hooks.add_after(on_start, AgentEventKind.AGENT_START)
        await hooks.run_after(_agent_start_event())
        assert fired == [AgentEventKind.AGENT_START]
