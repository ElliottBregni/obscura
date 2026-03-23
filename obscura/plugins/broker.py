"""Tool broker — single execution choke point for all plugin tool calls.

Every tool invocation flows through the broker, which enforces:

1. Schema validation (parameters match declared JSON Schema)
2. Policy check (capability + tool level via ``PluginPolicyEngine``)
3. Approval gate (if required)
4. Execution routing (resolve handler, invoke)
5. Timeout / retry handling
6. Structured result normalization
7. Audit event emission
8. Error normalization

Usage::

    from obscura.plugins.broker import ToolBroker

    broker = ToolBroker(policy_engine=engine, capability_resolver=resolver)
    result = await broker.execute(envelope)
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

from obscura.core.types import (
    ToolCallEnvelope,
    ToolResultEnvelope,
    ToolExecutionError,
    ToolErrorType,
)
from obscura.plugins.capabilities import CapabilityResolver
from obscura.plugins.policy import PluginPolicyEngine

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Registration validation
# ---------------------------------------------------------------------------


@dataclass
class RegistrationIssue:
    """A single problem found during tool registration validation."""

    level: str  # "critical" | "warning"
    message: str


@dataclass
class RegistrationResult:
    """Outcome of a tool registration attempt."""

    status: str  # "registered" | "quarantined"
    issues: list[RegistrationIssue] = field(default_factory=list)

    @property
    def has_critical(self) -> bool:
        return any(i.level == "critical" for i in self.issues)

    @property
    def summary(self) -> str:
        return "; ".join(i.message for i in self.issues)


# ---------------------------------------------------------------------------
# Approval callback protocol
# ---------------------------------------------------------------------------

ApprovalCallback = Callable[[ToolCallEnvelope, str], Awaitable[bool]]
"""Async callable: (envelope, reason) → True if approved."""


async def _auto_deny(_envelope: ToolCallEnvelope, reason: str) -> bool:
    """Default approval callback — always denies."""
    logger.warning("Approval required but no callback set — denying: %s", reason)
    return False


# ---------------------------------------------------------------------------
# Audit event
# ---------------------------------------------------------------------------


@dataclass
class BrokerAuditEntry:
    """Structured audit record emitted for every broker invocation."""

    call_id: str
    tool: str
    agent_id: str
    action: str  # "executed" | "denied" | "approval_denied" | "error" | "timeout"
    matched_rule: str = ""
    latency_ms: int = 0
    error: str = ""
    timestamp: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Broker
# ---------------------------------------------------------------------------


class ToolBroker:
    """Single choke-point for plugin tool execution.

    Parameters
    ----------
    policy_engine : PluginPolicyEngine
        Evaluates allow/deny/approve rules.
    capability_resolver : CapabilityResolver | None
        If provided, also checks capability grants before execution.
    approval_callback : ApprovalCallback | None
        Async function to call when user approval is required.
    default_timeout : float
        Default per-tool timeout in seconds.
    max_retries : int
        Maximum retry attempts on transient failures.
    """

    def __init__(
        self,
        policy_engine: PluginPolicyEngine,
        capability_resolver: CapabilityResolver | None = None,
        approval_callback: ApprovalCallback | None = None,
        default_timeout: float = 30.0,
        max_retries: int = 0,
        lazy_manager: Any | None = None,
        score_index: Any | None = None,
    ) -> None:
        self._policy = policy_engine
        self._resolver = capability_resolver
        self._approval = approval_callback or _auto_deny
        self._default_timeout = default_timeout
        self._max_retries = max_retries
        self._handlers: dict[str, Callable[..., Any]] = {}
        self._schemas: dict[str, dict[str, Any]] = {}
        self._audit_log: list[BrokerAuditEntry] = []
        self._specs: dict[str, Any] = {}
        self._lazy_manager = lazy_manager
        self._score_index = score_index
        self._quarantined: dict[str, tuple[Any, RegistrationResult]] = {}

    # -- Lazy loading --------------------------------------------------------

    def set_lazy_manager(self, manager: Any) -> None:
        """Attach a LazyPluginManager for on-demand handler resolution."""
        self._lazy_manager = manager

    # -- Handler registration ----------------------------------------------

    def register_handler(self, tool_name: str, handler: Callable[..., Any]) -> None:
        """Register an execution handler for a tool."""
        self._handlers[tool_name] = handler

    def register_tool(
        self,
        name: str,
        handler: Callable[..., Any],
        parameters: dict[str, Any] | None = None,
    ) -> None:
        """Register a tool with handler and optional parameter schema."""
        self._handlers[name] = handler
        if parameters is not None:
            self._schemas[name] = parameters

    def register_tool_spec(self, spec: Any) -> RegistrationResult:
        """Register a tool from its full ToolSpec, with quality gate.

        Validates the spec before adding it to the active registry.  Tools
        that fail critical checks are quarantined (stored but excluded from
        the active handler/schema dicts).
        """
        result = self._validate_registration(spec)
        if result.has_critical:
            self._quarantined[spec.name] = (spec, result)
            logger.warning("Tool %s quarantined: %s", spec.name, result.summary)
            return result

        self._handlers[spec.name] = spec.handler
        self._schemas[spec.name] = spec.parameters
        self._specs[spec.name] = spec

        if result.issues:
            logger.info(
                "Tool %s registered with warnings: %s", spec.name, result.summary
            )
        return result

    @property
    def quarantined_tools(self) -> list[str]:
        """Return names of quarantined (failed-validation) tools."""
        return list(self._quarantined.keys())

    @staticmethod
    def _validate_registration(spec: Any) -> RegistrationResult:
        """Run registration-time quality checks on a ToolSpec."""
        import re

        issues: list[RegistrationIssue] = []

        # Handler must be callable
        if not callable(getattr(spec, "handler", None)):
            issues.append(
                RegistrationIssue("critical", "handler is not callable")
            )

        # Name must be a valid identifier
        name = getattr(spec, "name", "")
        if not name or not re.match(r"^[a-zA-Z0-9_.:-]+$", name):
            issues.append(
                RegistrationIssue("critical", f"invalid tool name: {name!r}")
            )

        # Description should be present
        desc = getattr(spec, "description", "")
        if not desc:
            issues.append(
                RegistrationIssue("warning", "missing description")
            )

        # Parameters should be a dict (JSON Schema)
        params = getattr(spec, "parameters", None)
        if params is not None and not isinstance(params, dict):
            issues.append(
                RegistrationIssue("critical", "parameters is not a dict")
            )

        return RegistrationResult(
            status="quarantined" if any(i.level == "critical" for i in issues) else "registered",
            issues=issues,
        )

    def all_specs(self) -> list[Any]:
        """Return all registered ToolSpec objects."""
        return list(self._specs.values())

    # -- Introspection -----------------------------------------------------

    @property
    def schemas(self) -> dict[str, dict[str, Any]]:
        """Return a copy of all registered tool schemas."""
        return dict(self._schemas)

    @property
    def registered_tools(self) -> list[str]:
        """Return the names of all registered tool handlers."""
        return list(self._handlers.keys())

    # -- Main execution path -----------------------------------------------

    async def execute(self, envelope: ToolCallEnvelope) -> ToolResultEnvelope:
        """Execute a tool call through the full broker pipeline."""
        start = time.monotonic()

        # 1. Policy check
        decision = self._policy.can_execute_tool(
            envelope.tool, agent_id=envelope.agent_id
        )
        if not decision.allowed:
            return self._denied(envelope, decision.reason, decision.matched_rule, start)

        # 2. Capability check (if resolver available)
        if self._resolver is not None:
            if not self._check_capabilities(envelope):
                return self._denied(
                    envelope,
                    f"Agent {envelope.agent_id} lacks capability for tool {envelope.tool}",
                    "capability-check",
                    start,
                )

        # 3. Approval gate
        if decision.requires_approval:
            approved = await self._approval(envelope, decision.reason)
            if not approved:
                entry = BrokerAuditEntry(
                    call_id=envelope.call_id,
                    tool=envelope.tool,
                    agent_id=envelope.agent_id,
                    action="approval_denied",
                    matched_rule=decision.matched_rule,
                )
                self._record_audit(entry)
                return ToolResultEnvelope(
                    call_id=envelope.call_id,
                    tool=envelope.tool,
                    status="approval_denied",
                    error=ToolExecutionError(
                        type=ToolErrorType.UNAUTHORIZED,
                        message="User denied approval",
                    ),
                )

        # 4. Resolve handler (lazy-init plugin if needed)
        handler = self._handlers.get(envelope.tool)
        if handler is None and self._lazy_manager is not None:
            if self._lazy_manager.ensure_tool_ready(envelope.tool):
                handler = self._handlers.get(envelope.tool)
        if handler is None:
            return self._error(
                envelope, "no_handler", f"No handler for tool: {envelope.tool}", start
            )

        # 5. Execute with timeout and retry
        last_error: Exception | None = None
        attempts = 1 + self._max_retries
        for attempt in range(attempts):
            try:
                result = await self._invoke(handler, envelope)
                latency = int((time.monotonic() - start) * 1000)
                entry = BrokerAuditEntry(
                    call_id=envelope.call_id,
                    tool=envelope.tool,
                    agent_id=envelope.agent_id,
                    action="executed",
                    latency_ms=latency,
                )
                self._record_audit(entry)
                return ToolResultEnvelope(
                    call_id=envelope.call_id,
                    tool=envelope.tool,
                    status="ok",
                    result=result,
                    latency_ms=latency,
                )
            except asyncio.TimeoutError:
                last_error = asyncio.TimeoutError(f"Tool {envelope.tool} timed out")
                logger.warning(
                    "Timeout on %s (attempt %d/%d)",
                    envelope.tool,
                    attempt + 1,
                    attempts,
                )
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "Error on %s (attempt %d/%d): %s",
                    envelope.tool,
                    attempt + 1,
                    attempts,
                    exc,
                )

        # All retries exhausted
        latency = int((time.monotonic() - start) * 1000)
        error_msg = str(last_error) if last_error else "Unknown error"
        error_type = (
            ToolErrorType.TIMEOUT
            if isinstance(last_error, asyncio.TimeoutError)
            else ToolErrorType.UNKNOWN
        )
        entry = BrokerAuditEntry(
            call_id=envelope.call_id,
            tool=envelope.tool,
            agent_id=envelope.agent_id,
            action="timeout" if error_type == ToolErrorType.TIMEOUT else "error",
            latency_ms=latency,
            error=error_msg,
        )
        self._record_audit(entry)
        return ToolResultEnvelope(
            call_id=envelope.call_id,
            tool=envelope.tool,
            status="error",
            error=ToolExecutionError(
                type=error_type,
                message=error_msg,
                safe_to_retry=isinstance(last_error, asyncio.TimeoutError),
            ),
            latency_ms=latency,
        )

    # -- Internals ---------------------------------------------------------

    async def _invoke(
        self, handler: Callable[..., Any], envelope: ToolCallEnvelope
    ) -> Any:
        """Invoke handler with timeout."""
        coro = (
            handler(**envelope.args)
            if asyncio.iscoroutinefunction(handler)
            else asyncio.to_thread(handler, **envelope.args)
        )
        return await asyncio.wait_for(coro, timeout=self._default_timeout)

    def _check_capabilities(self, envelope: ToolCallEnvelope) -> bool:
        """Check if the agent has capability for the tool."""
        if self._resolver is None:
            return False
        visible = self._resolver.resolve_tool_names(envelope.agent_id)
        return envelope.tool in visible

    def _denied(
        self,
        envelope: ToolCallEnvelope,
        reason: str,
        rule: str,
        start: float,
    ) -> ToolResultEnvelope:
        latency = int((time.monotonic() - start) * 1000)
        entry = BrokerAuditEntry(
            call_id=envelope.call_id,
            tool=envelope.tool,
            agent_id=envelope.agent_id,
            action="denied",
            matched_rule=rule,
            latency_ms=latency,
        )
        self._record_audit(entry)
        return ToolResultEnvelope(
            call_id=envelope.call_id,
            tool=envelope.tool,
            status="denied",
            error=ToolExecutionError(
                type=ToolErrorType.UNAUTHORIZED,
                message=reason,
            ),
            latency_ms=latency,
        )

    def _error(
        self,
        envelope: ToolCallEnvelope,
        action: str,
        message: str,
        start: float,
    ) -> ToolResultEnvelope:
        latency = int((time.monotonic() - start) * 1000)
        entry = BrokerAuditEntry(
            call_id=envelope.call_id,
            tool=envelope.tool,
            agent_id=envelope.agent_id,
            action=action,
            latency_ms=latency,
            error=message,
        )
        self._record_audit(entry)
        return ToolResultEnvelope(
            call_id=envelope.call_id,
            tool=envelope.tool,
            status="error",
            error=ToolExecutionError(
                type=ToolErrorType.UNKNOWN,
                message=message,
            ),
            latency_ms=latency,
        )

    # -- Score index -------------------------------------------------------

    def set_score_index(self, index: Any) -> None:
        """Attach a :class:`ToolScoreIndex` for live quality tracking."""
        self._score_index = index

    def _record_audit(self, entry: BrokerAuditEntry) -> None:
        """Append audit entry and feed the score index if available."""
        self._audit_log.append(entry)
        if self._score_index is not None:
            self._score_index.record(entry)

    # -- Audit access ------------------------------------------------------

    @property
    def audit_log(self) -> list[BrokerAuditEntry]:
        return list(self._audit_log)


__all__ = [
    "ToolBroker",
    "BrokerAuditEntry",
    "ApprovalCallback",
    "RegistrationIssue",
    "RegistrationResult",
]
