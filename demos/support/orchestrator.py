"""
demos.support.orchestrator — Multi-agent pipeline coordinator.

Spawns TriageAgent → InvestigatorAgent → ResolutionAgent via AgentRuntime,
wires telemetry hooks, pipes output through the pipeline, and handles
escalation short-circuits.

Usage::

    from demos.support.orchestrator import SupportPipeline

    pipeline = SupportPipeline(user=authenticated_user)
    result = await pipeline.run("I was charged twice for my subscription (cust_001)")
    print(result.customer_message)
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from obscura.agent.agent import BaseAgent
from obscura.auth.models import AuthenticatedUser
from obscura.core.client import ObscuraClient
from obscura.core.types import HookPoint
from obscura.telemetry.hooks import register_telemetry_hooks

from demos.support.agents import (
    InvestigationResult,
    InvestigatorAgent,
    ResolutionAgent,
    ResolutionResult,
    TriageAgent,
    TriageResult,
)
from demos.support.tools import get_tool_specs

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pipeline result
# ---------------------------------------------------------------------------


@dataclass
class SupportResult:
    """Complete pipeline result with audit trail."""

    ticket: str
    triage: TriageResult
    investigation: InvestigationResult | None
    resolution: ResolutionResult | None
    escalated: bool
    total_time_ms: float
    phases_completed: list[str]
    hooks_fired: list[str]
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    @property
    def customer_message(self) -> str:
        if self.resolution:
            return self.resolution.customer_message
        if self.escalated:
            return (
                "Your issue has been escalated to our specialist team. "
                "A team member will reach out shortly."
            )
        return "We're looking into your issue and will get back to you soon."

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticket": self.ticket,
            "triage": self.triage.to_dict(),
            "investigation": self.investigation.to_dict() if self.investigation else None,
            "resolution": self.resolution.to_dict() if self.resolution else None,
            "escalated": self.escalated,
            "total_time_ms": self.total_time_ms,
            "phases_completed": self.phases_completed,
            "hooks_fired": self.hooks_fired,
            "timestamp": self.timestamp,
        }


# ---------------------------------------------------------------------------
# Hook tracker (for audit trail)
# ---------------------------------------------------------------------------


class HookTracker:
    """Records which hooks fire during the pipeline for auditing."""

    def __init__(self) -> None:
        self.fired: list[str] = []

    def make_callback(self, agent_name: str, hook_name: str) -> Any:
        def _cb(ctx: Any) -> None:
            entry = f"{agent_name}.{hook_name}"
            self.fired.append(entry)
            logger.debug("hook fired: %s", entry)
        return _cb


# ---------------------------------------------------------------------------
# SupportPipeline
# ---------------------------------------------------------------------------


class SupportPipeline:
    """Multi-agent customer support pipeline.

    Creates three agents (Triage → Investigate → Resolve), registers
    telemetry and audit hooks, and pipes data through the APER loop
    of each agent sequentially.

    Parameters
    ----------
    user:
        Authenticated user for capability token generation and memory scoping.
    backend:
        LLM backend name (default ``"copilot"``).
    model:
        Model override (optional).
    system_prompt_prefix:
        Prepended to each agent's system prompt.
    """

    def __init__(
        self,
        user: AuthenticatedUser,
        *,
        backend: str = "copilot",
        model: str | None = None,
        system_prompt_prefix: str = "",
    ) -> None:
        self._user = user
        self._backend = backend
        self._model = model
        self._system_prompt_prefix = system_prompt_prefix
        self._hook_tracker = HookTracker()

    # -- Agent factory ------------------------------------------------------

    def _create_agent(
        self,
        agent_cls: type[BaseAgent],
        system_prompt: str,
        name: str,
    ) -> tuple[ObscuraClient, BaseAgent]:
        """Create a client + agent pair with hooks registered."""
        full_prompt = (
            f"{self._system_prompt_prefix}\n\n{system_prompt}"
            if self._system_prompt_prefix
            else system_prompt
        )

        client = ObscuraClient(
            self._backend,
            model=self._model,
            system_prompt=full_prompt,
            tools=get_tool_specs(),
            user=self._user,
        )

        agent = agent_cls(client, name=name)

        # Register telemetry hooks (OTel spans + metrics)
        register_telemetry_hooks(agent)

        # Register audit hooks (for pipeline result)
        for hp in HookPoint:
            agent.on(hp, self._hook_tracker.make_callback(name, hp.value))

        return client, agent

    # -- Pipeline execution -------------------------------------------------

    async def run(self, ticket: str) -> SupportResult:
        """Execute the full triage → investigate → resolve pipeline.

        Parameters
        ----------
        ticket:
            The raw customer support ticket text.

        Returns
        -------
        SupportResult:
            Complete result with audit trail, metrics, and customer message.
        """
        start = time.monotonic()
        self._hook_tracker.fired.clear()
        phases_completed: list[str] = []

        # -- Phase 1: Triage ---------------------------------------------------
        logger.info("pipeline: starting triage")
        triage_client, triage_agent = self._create_agent(
            TriageAgent,
            system_prompt=(
                "You are a customer support triage agent. Your job is to classify "
                "incoming tickets by category (billing, technical, account, general) "
                "and severity (P1-P4). Extract customer IDs and detect urgency signals. "
                "Use query_customer and check_order_status tools to enrich the ticket."
            ),
            name="triage",
        )

        try:
            await triage_client.start()
            triage_result: TriageResult = await triage_agent.run(ticket)
            phases_completed.append("triage")
            logger.info(
                "pipeline: triage complete — category=%s severity=%s routing=%s",
                triage_result.category,
                triage_result.severity,
                triage_result.routing,
            )
        finally:
            await triage_client.stop()

        # -- Short-circuit: direct escalation ----------------------------------
        if triage_result.routing == "escalate":
            logger.info("pipeline: P1 escalation — skipping investigation/resolution")
            elapsed = (time.monotonic() - start) * 1000
            return SupportResult(
                ticket=ticket,
                triage=triage_result,
                investigation=None,
                resolution=None,
                escalated=True,
                total_time_ms=elapsed,
                phases_completed=phases_completed,
                hooks_fired=list(self._hook_tracker.fired),
            )

        # -- Phase 2: Investigation --------------------------------------------
        logger.info("pipeline: starting investigation")
        invest_client, invest_agent = self._create_agent(
            InvestigatorAgent,
            system_prompt=(
                "You are a support investigation agent. Given a triaged ticket, "
                "search past tickets and the knowledge base to find the root cause. "
                "Use search_tickets and search_knowledge_base tools. Build a "
                "diagnosis with recommended resolution steps."
            ),
            name="investigator",
        )

        try:
            await invest_client.start()
            investigation_result: InvestigationResult = await invest_agent.run(
                triage_result
            )
            phases_completed.append("investigation")
            logger.info(
                "pipeline: investigation complete — root_cause=%s escalate=%s",
                investigation_result.root_cause[:60],
                investigation_result.should_escalate,
            )
        finally:
            await invest_client.stop()

        # -- Short-circuit: investigator recommends escalation -----------------
        if investigation_result.should_escalate:
            logger.info("pipeline: investigator flagged escalation")
            elapsed = (time.monotonic() - start) * 1000
            return SupportResult(
                ticket=ticket,
                triage=triage_result,
                investigation=investigation_result,
                resolution=None,
                escalated=True,
                total_time_ms=elapsed,
                phases_completed=phases_completed,
                hooks_fired=list(self._hook_tracker.fired),
            )

        # -- Phase 3: Resolution -----------------------------------------------
        logger.info("pipeline: starting resolution")
        resolve_client, resolve_agent = self._create_agent(
            ResolutionAgent,
            system_prompt=(
                "You are a customer support resolution agent. Given investigation "
                "findings, draft a professional, empathetic customer-facing response. "
                "Choose the right tone (apology, fix, escalation, info). Ensure no "
                "internal jargon or PII leaks into the response."
            ),
            name="resolution",
        )

        try:
            await resolve_client.start()
            resolution_result: ResolutionResult = await resolve_agent.run(
                investigation_result
            )
            phases_completed.append("resolution")
            logger.info(
                "pipeline: resolution complete — type=%s time_ms=%.1f",
                resolution_result.response_type,
                resolution_result.resolution_time_ms,
            )
        finally:
            await resolve_client.stop()

        elapsed = (time.monotonic() - start) * 1000
        return SupportResult(
            ticket=ticket,
            triage=triage_result,
            investigation=investigation_result,
            resolution=resolution_result,
            escalated=False,
            total_time_ms=elapsed,
            phases_completed=phases_completed,
            hooks_fired=list(self._hook_tracker.fired),
        )
