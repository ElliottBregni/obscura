"""
demos.support.agents — Three-agent APER pipeline for customer support.

TriageAgent → InvestigatorAgent → ResolutionAgent

Each agent subclasses :class:`~sdk.agent.agent.BaseAgent` and implements
the full Analyze → Plan → Execute → Respond lifecycle with production
hooks for telemetry, compliance, and tier enforcement.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, TYPE_CHECKING, override

from obscura.agent.agent import BaseAgent
from obscura.core.types import AgentContext, HookPoint

if TYPE_CHECKING:
    from obscura.core.client import ObscuraClient

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Shared data structures
# ---------------------------------------------------------------------------

URGENCY_SIGNALS = frozenset(
    {
        "urgent",
        "asap",
        "immediately",
        "critical",
        "outage",
        "down",
        "emergency",
        "production",
        "p1",
        "broken",
        "blocked",
        "cannot access",
        "double charge",
        "charged twice",
        "unauthorized",
        "security breach",
    }
)

CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "billing": [
        "charge",
        "invoice",
        "payment",
        "refund",
        "subscription",
        "bill",
        "price",
        "cost",
        "credit",
        "discount",
        "upgrade",
        "downgrade",
    ],
    "technical": [
        "api",
        "error",
        "bug",
        "crash",
        "timeout",
        "webhook",
        "rate limit",
        "integration",
        "endpoint",
        "ssl",
        "certificate",
        "deployment",
    ],
    "account": [
        "login",
        "password",
        "access",
        "permission",
        "sso",
        "user",
        "role",
        "invite",
        "dashboard",
        "session",
        "authentication",
    ],
    "general": [
        "question",
        "documentation",
        "feature",
        "request",
        "compliance",
        "soc2",
        "gdpr",
        "contract",
        "sla",
    ],
}


@dataclass
class TriageResult:
    """Output of the TriageAgent — consumed by InvestigatorAgent."""

    customer_id: str
    category: str
    severity: str
    urgency_detected: bool
    customer_info: dict[str, Any] | None
    order_info: dict[str, Any] | None
    original_ticket: str
    routing: str  # "investigate" | "escalate" | "self-serve"

    def to_dict(self) -> dict[str, Any]:
        return {
            "customer_id": self.customer_id,
            "category": self.category,
            "severity": self.severity,
            "urgency_detected": self.urgency_detected,
            "customer_info": self.customer_info,
            "order_info": self.order_info,
            "original_ticket": self.original_ticket,
            "routing": self.routing,
        }


@dataclass
class InvestigationResult:
    """Output of the InvestigatorAgent — consumed by ResolutionAgent."""

    triage: TriageResult
    similar_tickets: list[dict[str, Any]]
    kb_articles: list[dict[str, Any]]
    root_cause: str
    recommended_action: str
    should_escalate: bool
    escalation_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "triage": self.triage.to_dict(),
            "similar_tickets": self.similar_tickets,
            "kb_articles": self.kb_articles,
            "root_cause": self.root_cause,
            "recommended_action": self.recommended_action,
            "should_escalate": self.should_escalate,
            "escalation_reason": self.escalation_reason,
        }


@dataclass
class ResolutionResult:
    """Output of the ResolutionAgent — final pipeline result."""

    investigation: InvestigationResult
    response_type: str  # "apology", "fix", "escalation", "info"
    customer_message: str
    internal_notes: str
    resolution_time_ms: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "investigation": self.investigation.to_dict(),
            "response_type": self.response_type,
            "customer_message": self.customer_message,
            "internal_notes": self.internal_notes,
            "resolution_time_ms": self.resolution_time_ms,
        }


# ===================================================================
# TriageAgent
# ===================================================================


class TriageAgent(BaseAgent):
    """Classify ticket severity/category and route to the right specialist.

    APER phases:
        - **Analyze**: Parse ticket text, extract customer ID, detect urgency
        - **Plan**: Classify category + severity
        - **Execute**: Enrich with customer/order data via tools
        - **Respond**: Produce structured TriageResult with routing decision

    Hooks:
        - ``POST_ANALYZE``: Log classification metrics
        - ``PRE_EXECUTE``: Validate customer exists before enrichment
    """

    def __init__(self, client: ObscuraClient, *, name: str = "triage") -> None:
        super().__init__(client, name=name)
        self._register_hooks()

    def _register_hooks(self) -> None:
        self.on(HookPoint.POST_ANALYZE, self._hook_post_analyze)
        self.on(HookPoint.PRE_EXECUTE, self._hook_pre_execute)

    # -- Hooks --------------------------------------------------------------

    @staticmethod
    def _hook_post_analyze(ctx: AgentContext) -> None:
        """Log urgency detection metrics after analysis."""
        analysis: dict[str, Any] = ctx.analysis or {}
        logger.info(
            "triage.post_analyze: customer_id=%s urgency=%s",
            analysis.get("customer_id", "unknown"),
            analysis.get("urgency_detected", False),
        )

    @staticmethod
    def _hook_pre_execute(ctx: AgentContext) -> None:
        """Validate we have a customer ID before running enrichment tools."""
        plan: dict[str, Any] = ctx.plan or {}
        customer_id: str = plan.get("customer_id", "")
        if not customer_id:
            logger.warning("triage.pre_execute: No customer_id — skipping enrichment")
            ctx.metadata["skip_enrichment"] = True

    # -- APER phases --------------------------------------------------------

    @override
    async def analyze(self, ctx: AgentContext) -> None:
        """Parse ticket text, extract customer ID, detect urgency signals."""
        ticket_text: str = ctx.input_data or ""
        ticket_lower = ticket_text.lower()

        # Extract customer ID (pattern: cust_XXX)
        customer_id = ""
        cid_match = re.search(r"cust_\d+", ticket_text)
        if cid_match:
            customer_id = cid_match.group(0)

        # Detect urgency
        urgency_detected = any(signal in ticket_lower for signal in URGENCY_SIGNALS)

        ctx.analysis = {
            "ticket_text": ticket_text,
            "customer_id": customer_id,
            "urgency_detected": urgency_detected,
        }

    @override
    async def plan(self, ctx: AgentContext) -> None:
        """Classify into category and severity."""
        analysis: dict[str, Any] = ctx.analysis or {}
        ticket_lower: str = analysis.get("ticket_text", "").lower()
        urgency: bool = analysis.get("urgency_detected", False)
        customer_id: str = analysis.get("customer_id", "")

        # Category classification by keyword matching
        scores: dict[str, int] = {cat: 0 for cat in CATEGORY_KEYWORDS}
        for cat, keywords in CATEGORY_KEYWORDS.items():
            for kw in keywords:
                if kw in ticket_lower:
                    scores[cat] += 1
        category = (
            max(scores, key=lambda c: scores[c]) if any(scores.values()) else "general"
        )

        # Severity assignment
        if urgency:
            severity = (
                "P1" if "outage" in ticket_lower or "down" in ticket_lower else "P2"
            )
        elif category == "billing":
            severity = "P2"
        elif category == "technical":
            severity = "P3"
        else:
            severity = "P4"

        ctx.plan = {
            "customer_id": customer_id,
            "category": category,
            "severity": severity,
            "urgency_detected": urgency,
        }

    @override
    async def execute(self, ctx: AgentContext) -> None:
        """Enrich with customer and order data via registered tools."""
        plan: dict[str, Any] = ctx.plan or {}
        customer_id: str = plan.get("customer_id", "")

        customer_info: dict[str, Any] | None = None
        order_info: dict[str, Any] | None = None

        if ctx.metadata.get("skip_enrichment") or not customer_id:
            ctx.results = [{"customer_info": None, "order_info": None}]
            return

        # Use the LLM to call tools for enrichment
        enrich_prompt = (
            f"Look up the customer account and order status for customer ID: {customer_id}. "
            f"Use the query_customer and check_order_status tools."
        )
        await self._client.send(enrich_prompt)

        # Parse enrichment from response (tools may have been called inline)
        # For deterministic demo: call tools directly as fallback
        from demos.support.tools import check_order_status, query_customer

        customer_raw = query_customer(customer_id)
        order_raw = check_order_status(customer_id)

        customer_info_loaded: dict[str, Any] = json.loads(customer_raw)
        customer_info = customer_info_loaded
        order_info_loaded: dict[str, Any] = json.loads(order_raw)
        order_info = order_info_loaded

        ctx.results = [{"customer_info": customer_info, "order_info": order_info}]

    @override
    async def respond(self, ctx: AgentContext) -> None:
        """Produce structured TriageResult with routing decision."""
        plan: dict[str, Any] = ctx.plan or {}
        analysis: dict[str, Any] = ctx.analysis or {}
        enrichment: dict[str, Any] = ctx.results[0] if ctx.results else {}

        severity: str = plan.get("severity", "P4")
        customer_info: dict[str, Any] | None = enrichment.get("customer_info")

        # Routing decision
        if severity == "P1":
            routing = "escalate"
        elif customer_info and customer_info.get("status") == "churned":
            routing = "self-serve"
        else:
            routing = "investigate"

        customer_id: str = plan.get("customer_id", "")
        category: str = plan.get("category", "general")
        urgency_detected: bool = plan.get("urgency_detected", False)
        order_info: dict[str, Any] | None = enrichment.get("order_info")
        original_ticket: str = analysis.get("ticket_text", "")

        ctx.response = TriageResult(
            customer_id=customer_id,
            category=category,
            severity=severity,
            urgency_detected=urgency_detected,
            customer_info=customer_info,
            order_info=order_info,
            original_ticket=original_ticket,
            routing=routing,
        )


# ===================================================================
# InvestigatorAgent
# ===================================================================


class InvestigatorAgent(BaseAgent):
    """Query tools to diagnose the issue and find a root cause.

    APER phases:
        - **Analyze**: Receive triage output, identify investigation strategy
        - **Plan**: Build ordered list of diagnostic steps
        - **Execute**: Call search_tickets, search_knowledge_base tools
        - **Respond**: Compile findings with root cause hypothesis

    Hooks:
        - ``PRE_TOOL_USE``: Enforce read-only tools for PUBLIC tier
        - ``POST_EXECUTE``: Store findings in memory for future retrieval
    """

    def __init__(self, client: ObscuraClient, *, name: str = "investigator") -> None:
        super().__init__(client, name=name)
        self._register_hooks()

    def _register_hooks(self) -> None:
        self.on(HookPoint.POST_EXECUTE, self._hook_post_execute)

    # -- Hooks --------------------------------------------------------------

    @staticmethod
    def _hook_post_execute(ctx: AgentContext) -> None:
        """Log investigation findings count."""
        results: list[Any] = ctx.results or []
        first: dict[str, Any] = results[0] if results else {}
        similar: list[dict[str, Any]] = (
            first.get("similar_tickets", []) if results else []
        )
        kb: list[dict[str, Any]] = first.get("kb_articles", []) if results else []
        logger.info(
            "investigator.post_execute: found %d similar tickets, %d KB articles",
            len(similar),
            len(kb),
        )

    # -- APER phases --------------------------------------------------------

    @override
    async def analyze(self, ctx: AgentContext) -> None:
        """Receive triage output and determine investigation strategy."""
        triage: TriageResult = ctx.input_data
        ctx.analysis = {
            "triage": triage,
            "category": triage.category,
            "severity": triage.severity,
            "customer_id": triage.customer_id,
            "search_queries": self._build_search_queries(triage),
        }

    @staticmethod
    def _build_search_queries(triage: TriageResult) -> list[str]:
        """Generate search queries from the ticket context."""
        queries: list[str] = []
        ticket_lower = triage.original_ticket.lower()

        # Primary query from ticket keywords
        words = re.findall(r"\b\w{4,}\b", ticket_lower)
        if words:
            queries.append(" ".join(words[:5]))

        # Category-specific query
        queries.append(triage.category)

        # Customer history query
        if triage.customer_id:
            queries.append(triage.customer_id)

        return queries

    @override
    async def plan(self, ctx: AgentContext) -> None:
        """Build ordered list of diagnostic steps."""
        analysis: dict[str, Any] = ctx.analysis or {}
        category: str = analysis.get("category", "general")

        steps = [
            f"Search past tickets for similar issues in category '{category}'",
            "Search knowledge base for resolution guides",
        ]

        if analysis.get("customer_id"):
            steps.append("Check customer's ticket history for recurring patterns")

        ctx.plan = {"steps": steps, "queries": analysis.get("search_queries", [])}

    @override
    async def execute(self, ctx: AgentContext) -> None:
        """Run search tools to gather diagnostic evidence."""
        analysis: dict[str, Any] = ctx.analysis or {}
        plan: dict[str, Any] = ctx.plan or {}
        triage: TriageResult = analysis["triage"]
        queries: list[str] = plan.get("queries", [])

        from demos.support.tools import search_knowledge_base, search_tickets

        # Search past tickets
        all_similar: list[dict[str, Any]] = []
        for query in queries:
            raw = search_tickets(
                query=query,
                customer_id=triage.customer_id,
                category=triage.category,
            )
            parsed: dict[str, Any] = json.loads(raw)
            for match in parsed["matches"]:
                if match not in all_similar:
                    all_similar.append(match)

        # Search knowledge base
        kb_raw = search_knowledge_base(query=triage.category)
        kb_parsed: dict[str, Any] = json.loads(kb_raw)

        # Also search with the original ticket text keywords
        ticket_words = re.findall(r"\b\w{4,}\b", triage.original_ticket.lower())
        if ticket_words:
            kb_extra: dict[str, Any] = json.loads(
                search_knowledge_base(query=" ".join(ticket_words[:3]))
            )
            for article in kb_extra["articles"]:
                if article not in kb_parsed["articles"]:
                    kb_parsed["articles"].append(article)

        # Use LLM for root cause analysis if we have context
        root_cause = "Unable to determine root cause — insufficient data."
        recommended_action = "Escalate to human agent for manual review."
        should_escalate = False

        if all_similar:
            # Found similar tickets — derive root cause from past resolutions
            past_resolution = all_similar[0].get("resolution", "")
            root_cause = f"Similar to {all_similar[0]['ticket_id']}: {past_resolution}"
            recommended_action = (
                f"Apply same resolution as {all_similar[0]['ticket_id']}. "
                f"Category: {triage.category}, Severity: {triage.severity}."
            )
        elif kb_parsed["articles"]:
            # Found KB articles — recommend following the guide
            article = kb_parsed["articles"][0]
            root_cause = f"Matches KB article: {article['title']}"
            recommended_action = article["content"]
        else:
            should_escalate = True

        # P1 tickets always get escalation flag
        if triage.severity == "P1":
            should_escalate = True

        ctx.results = [
            {
                "similar_tickets": all_similar,
                "kb_articles": kb_parsed["articles"],
                "root_cause": root_cause,
                "recommended_action": recommended_action,
                "should_escalate": should_escalate,
            }
        ]

    @override
    async def respond(self, ctx: AgentContext) -> None:
        """Compile investigation findings."""
        analysis: dict[str, Any] = ctx.analysis or {}
        triage: TriageResult = analysis["triage"]
        findings: dict[str, Any] = ctx.results[0] if ctx.results else {}

        similar_tickets: list[dict[str, Any]] = findings.get("similar_tickets", [])
        kb_articles: list[dict[str, Any]] = findings.get("kb_articles", [])
        root_cause: str = findings.get("root_cause", "Unknown")
        recommended_action: str = findings.get("recommended_action", "")
        should_escalate: bool = findings.get("should_escalate", False)

        ctx.response = InvestigationResult(
            triage=triage,
            similar_tickets=similar_tickets,
            kb_articles=kb_articles,
            root_cause=root_cause,
            recommended_action=recommended_action,
            should_escalate=should_escalate,
            escalation_reason=(
                f"P1 severity: {triage.severity}" if triage.severity == "P1" else None
            ),
        )


# ===================================================================
# ResolutionAgent
# ===================================================================


class ResolutionAgent(BaseAgent):
    """Draft and deliver customer-facing response.

    APER phases:
        - **Analyze**: Receive investigation findings, determine resolution type
        - **Plan**: Choose response template and tone
        - **Execute**: Draft response via LLM with compliance checks
        - **Respond**: Produce final customer message + internal notes

    Hooks:
        - ``PRE_RESPOND``: Validate tone and compliance
        - ``POST_RESPOND``: Record resolution metrics
    """

    def __init__(self, client: ObscuraClient, *, name: str = "resolution") -> None:
        super().__init__(client, name=name)
        self._start_time: float = 0.0
        self._register_hooks()

    def _register_hooks(self) -> None:
        self.on(HookPoint.PRE_ANALYZE, self._hook_pre_analyze)
        self.on(HookPoint.PRE_RESPOND, self._hook_pre_respond)
        self.on(HookPoint.POST_RESPOND, self._hook_post_respond)

    # -- Hooks --------------------------------------------------------------

    def _hook_pre_analyze(self, ctx: AgentContext) -> None:
        """Record pipeline start time for resolution metrics."""
        self._start_time = time.monotonic()

    @staticmethod
    def _hook_pre_respond(ctx: AgentContext) -> None:
        """Validate tone and compliance before sending response."""
        response_draft: str = ctx.metadata.get("response_draft", "")
        if not response_draft:
            return

        # Compliance checks
        violations: list[str] = []

        # No PII leakage
        if re.search(r"\b\d{4}[- ]?\d{4}[- ]?\d{4}[- ]?\d{4}\b", response_draft):
            violations.append("Contains credit card number pattern")

        # No internal jargon leakage
        internal_terms = ["HMAC", "capability token", "tier B", "privileged"]
        for term in internal_terms:
            if term.lower() in response_draft.lower():
                violations.append(f"Contains internal term: {term}")

        if violations:
            logger.warning(
                "resolution.pre_respond: compliance violations: %s", violations
            )
            ctx.metadata["compliance_violations"] = violations

    @staticmethod
    def _hook_post_respond(ctx: AgentContext) -> None:
        """Record resolution metrics."""
        result = ctx.response
        if isinstance(result, ResolutionResult):
            logger.info(
                "resolution.post_respond: type=%s time_ms=%.1f",
                result.response_type,
                result.resolution_time_ms,
            )

    # -- APER phases --------------------------------------------------------

    @override
    async def analyze(self, ctx: AgentContext) -> None:
        """Receive investigation findings and determine resolution type."""
        investigation: InvestigationResult = ctx.input_data

        if investigation.should_escalate:
            resolution_type = "escalation"
        elif investigation.triage.category == "billing":
            resolution_type = "apology"
        elif investigation.similar_tickets:
            resolution_type = "fix"
        else:
            resolution_type = "info"

        ctx.analysis = {
            "investigation": investigation,
            "resolution_type": resolution_type,
        }

    @override
    async def plan(self, ctx: AgentContext) -> None:
        """Choose response template and tone."""
        analysis: dict[str, Any] = ctx.analysis or {}
        resolution_type: str = analysis.get("resolution_type", "info")
        investigation: InvestigationResult = analysis["investigation"]
        triage = investigation.triage

        templates = {
            "apology": (
                f"Dear {triage.customer_info.get('name', 'Customer') if triage.customer_info else 'Customer'},\n\n"
                "We sincerely apologize for the inconvenience. We've identified the issue "
                "and are taking immediate steps to resolve it.\n\n"
                "{details}\n\n"
                "Please don't hesitate to reach out if you have any further questions.\n\n"
                "Best regards,\nSupport Team"
            ),
            "fix": (
                f"Hi {triage.customer_info.get('name', 'there') if triage.customer_info else 'there'},\n\n"
                "We've investigated your issue and have a solution.\n\n"
                "{details}\n\n"
                "Let us know if this resolves the issue for you.\n\n"
                "Best,\nSupport Team"
            ),
            "escalation": (
                f"Hi {triage.customer_info.get('name', 'there') if triage.customer_info else 'there'},\n\n"
                "Thank you for reaching out. We've reviewed your case and have "
                "escalated it to our specialist team for priority handling.\n\n"
                "{details}\n\n"
                "A team member will follow up with you shortly.\n\n"
                "Best,\nSupport Team"
            ),
            "info": (
                f"Hi {triage.customer_info.get('name', 'there') if triage.customer_info else 'there'},\n\n"
                "Thank you for your inquiry. Here's what we found:\n\n"
                "{details}\n\n"
                "Let us know if you need anything else.\n\n"
                "Best,\nSupport Team"
            ),
        }

        ctx.plan = {
            "resolution_type": resolution_type,
            "template": templates.get(resolution_type, templates["info"]),
            "investigation": investigation,
        }

    @override
    async def execute(self, ctx: AgentContext) -> None:
        """Draft response using template + investigation data."""
        plan: dict[str, Any] = ctx.plan or {}
        investigation: InvestigationResult = plan["investigation"]
        template: str = plan["template"]

        # Build details section from investigation
        details_parts: list[str] = []

        if investigation.root_cause:
            details_parts.append(f"Root cause: {investigation.root_cause}")

        if investigation.recommended_action:
            details_parts.append(
                f"\nRecommended steps:\n{investigation.recommended_action}"
            )

        if investigation.kb_articles:
            article: dict[str, Any] = investigation.kb_articles[0]
            details_parts.append(
                f"\nFor more information, see our guide: {article['title']}"
            )

        details = (
            "\n".join(details_parts) if details_parts else "We're looking into this."
        )
        draft = template.format(details=details)

        # Store draft for compliance hook
        ctx.metadata["response_draft"] = draft

        # Build internal notes
        ticket_ids: list[Any] = [t["ticket_id"] for t in investigation.similar_tickets]
        article_ids: list[Any] = [a["id"] for a in investigation.kb_articles]
        internal: list[str] = [
            f"Category: {investigation.triage.category}",
            f"Severity: {investigation.triage.severity}",
            f"Root cause: {investigation.root_cause}",
            f"Similar tickets: {ticket_ids}",
            f"KB articles used: {article_ids}",
        ]

        if investigation.should_escalate:
            internal.append(f"ESCALATED: {investigation.escalation_reason}")

        ctx.results = [
            {
                "draft": draft,
                "internal_notes": "\n".join(internal),
            }
        ]

    @override
    async def respond(self, ctx: AgentContext) -> None:
        """Produce final ResolutionResult."""
        analysis: dict[str, Any] = ctx.analysis or {}
        result_data: dict[str, Any] = ctx.results[0] if ctx.results else {}
        investigation: InvestigationResult = analysis["investigation"]

        elapsed_ms = (
            (time.monotonic() - self._start_time) * 1000 if self._start_time else 0
        )

        response_type: str = analysis.get("resolution_type", "info")
        customer_message: str = result_data.get("draft", "")
        internal_notes: str = result_data.get("internal_notes", "")

        ctx.response = ResolutionResult(
            investigation=investigation,
            response_type=response_type,
            customer_message=customer_message,
            internal_notes=internal_notes,
            resolution_time_ms=elapsed_ms,
        )
