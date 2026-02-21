"""
demos.a2a.agents — Three A2A agent servers for customer support.

Each factory function returns a FastAPI app wired with:
    - ``InMemoryTaskStore`` for task persistence
    - ``AgentCardGenerator`` for agent discovery
    - A custom ``A2AService`` subclass with deterministic domain logic
    - Full transport stack (JSON-RPC, REST, SSE, well-known)

The agents reuse domain logic and mock data from ``demos.support``.
No LLM calls — all processing is deterministic, making the demo
runnable without API keys.

Usage::

    app = create_triage_app()
    # Mount in ASGI server or use with ASGITransport for testing
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any, AsyncIterator

from fastapi import FastAPI

from sdk.a2a.agent_card import AgentCardGenerator
from sdk.a2a.service import A2AService
from sdk.a2a.store import InMemoryTaskStore
from sdk.a2a.transports.jsonrpc import create_jsonrpc_router
from sdk.a2a.transports.rest import create_rest_router, create_wellknown_router
from sdk.a2a.transports.sse import create_sse_router
from sdk.a2a.types import (
    A2AMessage,
    AgentSkill,
    Artifact,
    Task,
    TaskState,
    TextPart,
)
from sdk.internal.types import AgentEvent, AgentEventKind

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Support domain imports (reuse from demos.support)
# ---------------------------------------------------------------------------

from demos.support.agents import (
    CATEGORY_KEYWORDS,
    URGENCY_SIGNALS,
    InvestigationResult,
    ResolutionResult,
    TriageResult,
)
from demos.support.tools import (
    check_order_status,
    query_customer,
    search_knowledge_base,
    search_tickets,
)


# ---------------------------------------------------------------------------
# Custom A2AService subclass — overrides _execute_agent for domain logic
# ---------------------------------------------------------------------------


class DomainA2AService(A2AService):
    """A2AService that delegates to a sync domain function instead of an LLM.

    The ``domain_fn`` receives the user message text and returns the
    result text (typically JSON). This makes agents fully deterministic.
    """

    def __init__(
        self,
        *args: Any,
        domain_fn: Any = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._domain_fn = domain_fn

    async def _execute_agent(self, task: Task, prompt: str) -> str:
        if self._domain_fn:
            return self._domain_fn(prompt)
        return f"[No domain function] Received: {prompt}"

    async def _execute_agent_stream(
        self, task: Task, prompt: str,
    ) -> AsyncIterator[AgentEvent]:
        """Stream agent execution as AgentEvent objects."""
        yield AgentEvent(kind=AgentEventKind.TURN_START)

        if self._domain_fn:
            result = self._domain_fn(prompt)
        else:
            result = f"[No domain function] Received: {prompt}"

        # Emit result in chunks for realistic streaming
        chunk_size = 120
        for i in range(0, len(result), chunk_size):
            yield AgentEvent(
                kind=AgentEventKind.TEXT_DELTA,
                text=result[i : i + chunk_size],
            )

        yield AgentEvent(kind=AgentEventKind.TURN_COMPLETE)
        yield AgentEvent(kind=AgentEventKind.AGENT_DONE)


# ---------------------------------------------------------------------------
# Triage domain logic
# ---------------------------------------------------------------------------


def _triage_fn(prompt: str) -> str:
    """Deterministic triage: classify, extract customer, detect urgency."""
    ticket_lower = prompt.lower()

    # Extract customer ID
    customer_id = ""
    cid_match = re.search(r"cust_\d+", prompt)
    if cid_match:
        customer_id = cid_match.group(0)

    # Detect urgency
    urgency_detected = any(signal in ticket_lower for signal in URGENCY_SIGNALS)

    # Category classification
    scores: dict[str, int] = {cat: 0 for cat in CATEGORY_KEYWORDS}
    for cat, keywords in CATEGORY_KEYWORDS.items():
        for kw in keywords:
            if kw in ticket_lower:
                scores[cat] += 1
    category = max(scores, key=lambda c: scores[c]) if any(scores.values()) else "general"

    # Severity
    if urgency_detected:
        severity = "P1" if "outage" in ticket_lower or "down" in ticket_lower else "P2"
    elif category == "billing":
        severity = "P2"
    elif category == "technical":
        severity = "P3"
    else:
        severity = "P4"

    # Customer enrichment
    customer_info = None
    order_info = None
    if customer_id:
        try:
            customer_info = json.loads(query_customer(customer_id))
            if "error" in customer_info:
                customer_info = None
        except Exception:
            pass
        try:
            order_data = json.loads(check_order_status(customer_id))
            if "error" not in order_data:
                order_info = order_data
        except Exception:
            pass

    # Routing
    if severity == "P1":
        routing = "escalate"
    elif customer_info and customer_info.get("status") == "churned":
        routing = "self-serve"
    else:
        routing = "investigate"

    result = TriageResult(
        customer_id=customer_id,
        category=category,
        severity=severity,
        urgency_detected=urgency_detected,
        customer_info=customer_info,
        order_info=order_info,
        original_ticket=prompt,
        routing=routing,
    )
    return json.dumps(result.to_dict())


# ---------------------------------------------------------------------------
# Investigator domain logic
# ---------------------------------------------------------------------------


def _investigator_fn(prompt: str) -> str:
    """Deterministic investigation: search tickets + KB, find root cause."""
    # Parse triage JSON from the prompt
    try:
        triage_data = json.loads(prompt)
    except json.JSONDecodeError:
        # Fallback: treat as raw text
        triage_data = {"category": "general", "customer_id": "", "original_ticket": prompt}

    category = triage_data.get("category", "general")
    customer_id = triage_data.get("customer_id", "")
    severity = triage_data.get("severity", "P4")
    original_ticket = triage_data.get("original_ticket", prompt)

    # Search past tickets
    all_similar: list[dict[str, Any]] = []
    for query in [category, customer_id]:
        if not query:
            continue
        raw = search_tickets(query=query, customer_id=customer_id, category=category)
        parsed = json.loads(raw)
        for match in parsed["matches"]:
            if match not in all_similar:
                all_similar.append(match)

    # Search knowledge base
    kb_raw = search_knowledge_base(query=category)
    kb_parsed = json.loads(kb_raw)

    # Also search with ticket keywords
    ticket_words = re.findall(r"\b\w{4,}\b", original_ticket.lower())
    if ticket_words:
        kb_extra = json.loads(search_knowledge_base(query=" ".join(ticket_words[:3])))
        for article in kb_extra["articles"]:
            if article not in kb_parsed["articles"]:
                kb_parsed["articles"].append(article)

    # Derive root cause
    root_cause = "Unable to determine root cause."
    recommended_action = "Escalate to human agent."
    should_escalate = False

    if all_similar:
        past_resolution = all_similar[0].get("resolution", "")
        root_cause = f"Similar to {all_similar[0]['ticket_id']}: {past_resolution}"
        recommended_action = (
            f"Apply same resolution as {all_similar[0]['ticket_id']}. "
            f"Category: {category}, Severity: {severity}."
        )
    elif kb_parsed["articles"]:
        article = kb_parsed["articles"][0]
        root_cause = f"Matches KB article: {article['title']}"
        recommended_action = article["content"]
    else:
        should_escalate = True

    if severity == "P1":
        should_escalate = True

    result = InvestigationResult(
        triage=TriageResult(**{k: triage_data.get(k) for k in [
            "customer_id", "category", "severity", "urgency_detected",
            "customer_info", "order_info", "original_ticket", "routing",
        ]}),
        similar_tickets=all_similar,
        kb_articles=kb_parsed["articles"],
        root_cause=root_cause,
        recommended_action=recommended_action,
        should_escalate=should_escalate,
        escalation_reason=f"P1 severity: {severity}" if severity == "P1" else None,
    )
    return json.dumps(result.to_dict())


# ---------------------------------------------------------------------------
# Resolution domain logic
# ---------------------------------------------------------------------------


def _resolution_fn(prompt: str) -> str:
    """Deterministic resolution: draft customer response from investigation."""
    try:
        inv_data = json.loads(prompt)
    except json.JSONDecodeError:
        inv_data = {}

    triage_data = inv_data.get("triage", {})
    customer_info = triage_data.get("customer_info")
    customer_name = (
        customer_info.get("name", "Customer") if customer_info else "Customer"
    )
    category = triage_data.get("category", "general")
    root_cause = inv_data.get("root_cause", "Under investigation")
    recommended_action = inv_data.get("recommended_action", "")
    kb_articles = inv_data.get("kb_articles", [])
    should_escalate = inv_data.get("should_escalate", False)
    similar_tickets = inv_data.get("similar_tickets", [])

    # Determine response type
    if should_escalate:
        response_type = "escalation"
    elif category == "billing":
        response_type = "apology"
    elif similar_tickets:
        response_type = "fix"
    else:
        response_type = "info"

    # Build details section
    details_parts: list[str] = []
    if root_cause:
        details_parts.append(f"Root cause: {root_cause}")
    if recommended_action:
        details_parts.append(f"\nRecommended steps:\n{recommended_action}")
    if kb_articles:
        details_parts.append(
            f"\nFor more information, see our guide: {kb_articles[0]['title']}"
        )
    details = "\n".join(details_parts) if details_parts else "We're looking into this."

    # Templates
    templates = {
        "apology": (
            f"Dear {customer_name},\n\n"
            "We sincerely apologize for the inconvenience. We've identified the issue "
            "and are taking immediate steps to resolve it.\n\n"
            f"{details}\n\n"
            "Please don't hesitate to reach out if you have any further questions.\n\n"
            "Best regards,\nSupport Team"
        ),
        "fix": (
            f"Hi {customer_name},\n\n"
            "We've investigated your issue and have a solution.\n\n"
            f"{details}\n\n"
            "Let us know if this resolves the issue for you.\n\n"
            "Best,\nSupport Team"
        ),
        "escalation": (
            f"Hi {customer_name},\n\n"
            "Thank you for reaching out. We've reviewed your case and have "
            "escalated it to our specialist team for priority handling.\n\n"
            f"{details}\n\n"
            "A team member will follow up with you shortly.\n\n"
            "Best,\nSupport Team"
        ),
        "info": (
            f"Hi {customer_name},\n\n"
            "Thank you for your inquiry. Here's what we found:\n\n"
            f"{details}\n\n"
            "Let us know if you need anything else.\n\n"
            "Best,\nSupport Team"
        ),
    }

    customer_message = templates.get(response_type, templates["info"])

    # Internal notes
    internal = [
        f"Category: {category}",
        f"Severity: {triage_data.get('severity', 'unknown')}",
        f"Root cause: {root_cause}",
        f"Similar tickets: {[t.get('ticket_id', '?') for t in similar_tickets]}",
        f"KB articles used: {[a.get('id', '?') for a in kb_articles]}",
    ]
    if should_escalate:
        internal.append(f"ESCALATED: {inv_data.get('escalation_reason', 'N/A')}")

    result = ResolutionResult(
        investigation=InvestigationResult(
            triage=TriageResult(**{k: triage_data.get(k) for k in [
                "customer_id", "category", "severity", "urgency_detected",
                "customer_info", "order_info", "original_ticket", "routing",
            ]}),
            similar_tickets=similar_tickets,
            kb_articles=kb_articles,
            root_cause=root_cause,
            recommended_action=recommended_action,
            should_escalate=should_escalate,
            escalation_reason=inv_data.get("escalation_reason"),
        ),
        response_type=response_type,
        customer_message=customer_message,
        internal_notes="\n".join(internal),
        resolution_time_ms=0.0,
    )
    return json.dumps(result.to_dict())


# ---------------------------------------------------------------------------
# FastAPI app factories
# ---------------------------------------------------------------------------


def _build_app(
    name: str,
    url: str,
    description: str,
    skills: list[AgentSkill],
    domain_fn: Any,
) -> FastAPI:
    """Build a FastAPI app wired with A2A transports + domain logic."""
    store = InMemoryTaskStore()
    card = (
        AgentCardGenerator(name, url, description=description)
        .with_skills(skills)
        .with_capabilities(streaming=True)
        .build()
    )
    service = DomainA2AService(
        store=store,
        agent_card=card,
        domain_fn=domain_fn,
    )

    app = FastAPI(title=name, description=description)
    app.include_router(create_jsonrpc_router(service))
    app.include_router(create_rest_router(service))
    app.include_router(create_wellknown_router(service))
    app.include_router(create_sse_router(service))

    # Stash service for test access
    app.state.a2a_service = service

    return app


def create_triage_app(url: str = "http://triage.local") -> FastAPI:
    """Create the Triage A2A agent as a FastAPI app.

    Skills: classify, extract_customer, detect_urgency
    """
    return _build_app(
        name="TriageAgent",
        url=url,
        description=(
            "Classifies support tickets by category (billing, technical, account, general) "
            "and severity (P1-P4). Extracts customer IDs, detects urgency, and enriches "
            "with customer/order data."
        ),
        skills=[
            AgentSkill(id="classify", name="classify", description="Classify ticket category and severity"),
            AgentSkill(id="extract_customer", name="extract_customer", description="Extract customer ID from ticket"),
            AgentSkill(id="detect_urgency", name="detect_urgency", description="Detect urgency signals in ticket text"),
        ],
        domain_fn=_triage_fn,
    )


def create_investigator_app(url: str = "http://investigator.local") -> FastAPI:
    """Create the Investigator A2A agent as a FastAPI app.

    Skills: search_similar, search_kb, root_cause
    """
    return _build_app(
        name="InvestigatorAgent",
        url=url,
        description=(
            "Investigates triaged tickets by searching past tickets and knowledge base. "
            "Identifies root causes and recommends resolution actions."
        ),
        skills=[
            AgentSkill(id="search_similar", name="search_similar", description="Search past tickets for similar issues"),
            AgentSkill(id="search_kb", name="search_kb", description="Search knowledge base articles"),
            AgentSkill(id="root_cause", name="root_cause", description="Determine root cause from evidence"),
        ],
        domain_fn=_investigator_fn,
    )


def create_resolution_app(url: str = "http://resolution.local") -> FastAPI:
    """Create the Resolution A2A agent as a FastAPI app.

    Skills: draft_response, send_response
    """
    return _build_app(
        name="ResolutionAgent",
        url=url,
        description=(
            "Drafts professional customer-facing responses based on investigation findings. "
            "Chooses appropriate tone (apology, fix, escalation, info)."
        ),
        skills=[
            AgentSkill(id="draft_response", name="draft_response", description="Draft customer-facing response message"),
            AgentSkill(id="send_response", name="send_response", description="Send the final response to the customer"),
        ],
        domain_fn=_resolution_fn,
    )
