"""obscura.core.lifecycle -- Built-in lifecycle hook factories.

Provides reusable hook factory functions that return standard
:data:`BeforeHook` / :data:`AfterHook` callables compatible with
:class:`HookRegistry`.

Usage::

    from obscura.core.lifecycle import make_policy_gate_hook, make_audit_hook
    from obscura.core.hooks import HookRegistry
    from obscura.core.types import AgentEventKind

    hooks = HookRegistry()
    hooks.add_before(make_policy_gate_hook(engine), AgentEventKind.TOOL_CALL)
    hooks.add_after(make_audit_hook(), None)  # wildcard
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Callable

from obscura.core.hooks import AfterHook, BeforeHook
from obscura.core.types import AgentEvent, AgentEventKind
from obscura.plugins.broker import BrokerAuditEntry
from obscura.plugins.policy import PluginPolicyEngine

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Policy gate
# ---------------------------------------------------------------------------


def make_policy_gate_hook(engine: PluginPolicyEngine) -> BeforeHook:
    """Create a before-hook that enforces policy on TOOL_CALL events.

    Returns ``None`` (suppressing the event) when the policy denies the
    tool.  Non-TOOL_CALL events pass through unmodified.
    """

    def _hook(event: AgentEvent) -> AgentEvent | None:
        if event.kind != AgentEventKind.TOOL_CALL:
            return event
        decision = engine.can_execute_tool(event.tool_name)
        if not decision.allowed:
            logger.warning(
                "Policy gate denied tool '%s': %s (rule: %s)",
                event.tool_name,
                decision.reason,
                decision.matched_rule,
            )
            return None
        return event

    return _hook


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


def make_audit_hook(store: list[BrokerAuditEntry] | None = None) -> AfterHook:
    """Create an after-hook that records structured audit entries.

    Appends a :class:`BrokerAuditEntry` for every event to *store*.
    If *store* is ``None``, a new list is created and attached as
    ``_hook.store`` for later inspection.
    """
    audit_store: list[BrokerAuditEntry] = store if store is not None else []

    def _hook(event: AgentEvent) -> None:
        entry = BrokerAuditEntry(
            call_id=event.tool_use_id or "",
            tool=event.tool_name or "",
            agent_id="",
            action=event.kind.value,
            timestamp=time.time(),
        )
        audit_store.append(entry)

    _hook.store = audit_store  # type: ignore[attr-defined]
    return _hook


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


def make_redact_hook(patterns: list[str]) -> AfterHook:
    """Create an after-hook that redacts secrets from TOOL_RESULT events.

    Compiles each pattern in *patterns* as a regex and replaces matches
    in ``event.tool_result`` with ``[REDACTED]``.  Only runs on
    TOOL_RESULT events.
    """
    compiled = [re.compile(p) for p in patterns]

    def _hook(event: AgentEvent) -> None:
        if event.kind != AgentEventKind.TOOL_RESULT:
            return
        if not event.tool_result:
            return
        result = event.tool_result
        for pattern in compiled:
            result = pattern.sub("[REDACTED]", result)
        event.tool_result = result

    return _hook


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------


def make_preflight_hook(
    validator: Any,  # PreflightValidator (avoid circular import)
) -> BeforeHook:
    """Create a before-hook that runs preflight on AGENT_START events.

    Suppresses the event (returns ``None``) if preflight fails, blocking
    agent startup.  Non-AGENT_START events pass through unmodified.

    Parameters
    ----------
    validator :
        A :class:`PreflightValidator` instance.  The hook calls
        ``validator.validate(agent)`` using agent info from the event.
    """

    def _hook(event: AgentEvent) -> AgentEvent | None:
        if event.kind != AgentEventKind.AGENT_START:
            return event
        # The validator needs a CompiledAgent; metadata may carry one
        agent = getattr(event, "_agent", None)
        if agent is None:
            return event
        result = validator.validate(agent)
        if not result.passed:
            logger.error(
                "Preflight failed for agent '%s': %s",
                result.agent_name,
                "; ".join(c.message for c in result.errors),
            )
            return None
        return event

    return _hook


# ---------------------------------------------------------------------------
# Memory injection
# ---------------------------------------------------------------------------


def make_memory_inject_hook(
    memory_loader: Callable[[], str],
) -> BeforeHook:
    """Create a before-hook that injects memory context into TURN_START.

    Calls *memory_loader* to get memory text and prepends it to
    ``event.text``.  Only runs on TURN_START events.
    """

    def _hook(event: AgentEvent) -> AgentEvent | None:
        if event.kind != AgentEventKind.TURN_START:
            return event
        try:
            memory = memory_loader()
            if memory:
                event.text = memory + "\n" + event.text if event.text else memory
        except Exception:
            logger.exception("Memory inject hook failed")
        return event

    return _hook


__all__ = [
    "make_policy_gate_hook",
    "make_audit_hook",
    "make_redact_hook",
    "make_preflight_hook",
    "make_memory_inject_hook",
]
