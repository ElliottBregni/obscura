"""Tests for demos.support — Customer support multi-agent pipeline."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from obscura.core.types import AgentContext, AgentPhase, HookPoint, ContentBlock, Message, Role

from demos.support.agents import (
    InvestigationResult,
    InvestigatorAgent,
    ResolutionAgent,
    ResolutionResult,
    TriageAgent,
    TriageResult,
)
from demos.support.tools import (
    check_order_status,
    escalate_to_human,
    get_tool_specs,
    query_customer,
    search_knowledge_base,
    search_tickets,
    send_response,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _mock_client() -> MagicMock:
    """Create a mock ObscuraClient that returns empty responses."""
    client = MagicMock()
    client.send = AsyncMock(
        return_value=Message(
            role=Role.ASSISTANT,
            content=[ContentBlock(kind="text", text="mock response")],
        )
    )
    client.start = AsyncMock()
    client.stop = AsyncMock()
    return client


def _sample_triage_result(
    customer_id: str = "cust_001",
    category: str = "billing",
    severity: str = "P2",
    routing: str = "investigate",
) -> TriageResult:
    return TriageResult(
        customer_id=customer_id,
        category=category,
        severity=severity,
        urgency_detected=False,
        customer_info={"id": customer_id, "name": "Acme Corp", "plan": "enterprise"},
        order_info={"customer_id": customer_id, "orders": []},
        original_ticket="I was charged twice for my subscription (cust_001)",
        routing=routing,
    )


def _sample_investigation_result(
    should_escalate: bool = False,
) -> InvestigationResult:
    triage = _sample_triage_result()
    return InvestigationResult(
        triage=triage,
        similar_tickets=[{
            "ticket_id": "TKT-1001",
            "subject": "Double charge on February invoice",
            "resolution": "Refund issued for duplicate charge.",
        }],
        kb_articles=[{
            "id": "kb_001",
            "title": "Handling Duplicate Charges",
            "content": "When a customer reports a duplicate charge...",
        }],
        root_cause="Similar to TKT-1001: Refund issued for duplicate charge.",
        recommended_action="Apply same resolution as TKT-1001.",
        should_escalate=should_escalate,
    )


# ===================================================================
# Tool tests
# ===================================================================


class TestTools:
    def test_search_tickets_finds_match(self) -> None:
        raw = search_tickets("double charge")
        result = json.loads(raw)
        assert result["total"] >= 1
        assert any("TKT-1001" == t["ticket_id"] for t in result["matches"])

    def test_search_tickets_filters_by_customer(self) -> None:
        raw = search_tickets("charge", customer_id="cust_002")
        result = json.loads(raw)
        assert result["total"] == 0

    def test_search_tickets_filters_by_category(self) -> None:
        raw = search_tickets("charge", category="technical")
        result = json.loads(raw)
        assert result["total"] == 0

    def test_query_customer_found(self) -> None:
        raw = query_customer("cust_001")
        result = json.loads(raw)
        assert result["name"] == "Acme Corp"
        assert result["plan"] == "enterprise"

    def test_query_customer_not_found(self) -> None:
        raw = query_customer("cust_999")
        result = json.loads(raw)
        assert "error" in result

    def test_check_order_status(self) -> None:
        raw = check_order_status("cust_001")
        result = json.loads(raw)
        assert len(result["orders"]) == 2

    def test_check_order_status_not_found(self) -> None:
        raw = check_order_status("cust_999")
        result = json.loads(raw)
        assert "error" in result

    def test_search_knowledge_base(self) -> None:
        raw = search_knowledge_base("duplicate charge")
        result = json.loads(raw)
        assert result["total"] >= 1
        assert any("kb_001" == a["id"] for a in result["articles"])

    def test_search_knowledge_base_by_category(self) -> None:
        raw = search_knowledge_base("rate", category="technical")
        result = json.loads(raw)
        assert all(a["category"] == "technical" for a in result["articles"])

    def test_escalate_to_human(self) -> None:
        raw = escalate_to_human("P1 outage", "P1", "engineering")
        result = json.loads(raw)
        assert result["severity"] == "P1"
        assert result["status"] == "pending_review"
        assert result["suggested_team"] == "engineering"

    def test_send_response(self) -> None:
        raw = send_response("cust_001", "Re: Your ticket", "We fixed it.", "internal note")
        result = json.loads(raw)
        assert result["customer_id"] == "cust_001"
        assert result["status"] == "delivered"

    def test_get_tool_specs_returns_all(self) -> None:
        specs = get_tool_specs()
        assert len(specs) == 6
        names = {s.name for s in specs}
        assert names == {
            "search_tickets", "query_customer", "check_order_status",
            "search_knowledge_base", "escalate_to_human", "send_response",
        }

    def test_send_response_requires_privileged_tier(self) -> None:
        specs = get_tool_specs()
        send_spec = next(s for s in specs if s.name == "send_response")
        assert send_spec.required_tier == "privileged"

    def test_search_tickets_is_public_tier(self) -> None:
        specs = get_tool_specs()
        search_spec = next(s for s in specs if s.name == "search_tickets")
        assert search_spec.required_tier == "public"


# ===================================================================
# TriageAgent tests
# ===================================================================


class TestTriageAgent:
    @pytest.mark.asyncio
    async def test_analyze_extracts_customer_id(self) -> None:
        agent = TriageAgent(_mock_client())
        ctx = AgentContext(phase=AgentPhase.ANALYZE, input_data="Problem with cust_001 billing")
        await agent.analyze(ctx)
        assert ctx.analysis["customer_id"] == "cust_001"

    @pytest.mark.asyncio
    async def test_analyze_detects_urgency(self) -> None:
        agent = TriageAgent(_mock_client())
        ctx = AgentContext(phase=AgentPhase.ANALYZE, input_data="URGENT: production is down!")
        await agent.analyze(ctx)
        assert ctx.analysis["urgency_detected"] is True

    @pytest.mark.asyncio
    async def test_analyze_no_urgency(self) -> None:
        agent = TriageAgent(_mock_client())
        ctx = AgentContext(phase=AgentPhase.ANALYZE, input_data="Can I get SOC2 docs?")
        await agent.analyze(ctx)
        assert ctx.analysis["urgency_detected"] is False

    @pytest.mark.asyncio
    async def test_plan_classifies_billing(self) -> None:
        agent = TriageAgent(_mock_client())
        ctx = AgentContext(phase=AgentPhase.PLAN)
        ctx.analysis = {
            "ticket_text": "I was charged twice for my subscription",
            "customer_id": "cust_001",
            "urgency_detected": False,
        }
        await agent.plan(ctx)
        assert ctx.plan["category"] == "billing"
        assert ctx.plan["severity"] == "P2"

    @pytest.mark.asyncio
    async def test_plan_classifies_technical(self) -> None:
        agent = TriageAgent(_mock_client())
        ctx = AgentContext(phase=AgentPhase.PLAN)
        ctx.analysis = {
            "ticket_text": "API endpoint returns timeout error",
            "customer_id": "cust_003",
            "urgency_detected": False,
        }
        await agent.plan(ctx)
        assert ctx.plan["category"] == "technical"

    @pytest.mark.asyncio
    async def test_plan_p1_on_outage(self) -> None:
        agent = TriageAgent(_mock_client())
        ctx = AgentContext(phase=AgentPhase.PLAN)
        ctx.analysis = {
            "ticket_text": "production outage everything is down",
            "customer_id": "cust_001",
            "urgency_detected": True,
        }
        await agent.plan(ctx)
        assert ctx.plan["severity"] == "P1"

    @pytest.mark.asyncio
    async def test_execute_enriches_with_customer_data(self) -> None:
        agent = TriageAgent(_mock_client())
        ctx = AgentContext(phase=AgentPhase.EXECUTE)
        ctx.plan = {"customer_id": "cust_001", "category": "billing", "severity": "P2"}
        ctx.metadata = {}
        await agent.execute(ctx)
        assert ctx.results[0]["customer_info"]["name"] == "Acme Corp"
        assert len(ctx.results[0]["order_info"]["orders"]) == 2

    @pytest.mark.asyncio
    async def test_execute_skips_enrichment_without_customer_id(self) -> None:
        agent = TriageAgent(_mock_client())
        ctx = AgentContext(phase=AgentPhase.EXECUTE)
        ctx.plan = {"customer_id": "", "category": "general", "severity": "P4"}
        ctx.metadata = {}
        await agent.execute(ctx)
        assert ctx.results[0]["customer_info"] is None

    @pytest.mark.asyncio
    async def test_respond_routes_p1_to_escalate(self) -> None:
        agent = TriageAgent(_mock_client())
        ctx = AgentContext(phase=AgentPhase.RESPOND)
        ctx.analysis = {"ticket_text": "outage"}
        ctx.plan = {
            "customer_id": "cust_001", "category": "technical",
            "severity": "P1", "urgency_detected": True,
        }
        ctx.results = [{"customer_info": {"status": "active"}, "order_info": None}]
        await agent.respond(ctx)
        result: TriageResult = ctx.response
        assert result.routing == "escalate"

    @pytest.mark.asyncio
    async def test_respond_routes_churned_to_self_serve(self) -> None:
        agent = TriageAgent(_mock_client())
        ctx = AgentContext(phase=AgentPhase.RESPOND)
        ctx.analysis = {"ticket_text": "question"}
        ctx.plan = {
            "customer_id": "cust_004", "category": "general",
            "severity": "P4", "urgency_detected": False,
        }
        ctx.results = [{"customer_info": {"status": "churned"}, "order_info": None}]
        await agent.respond(ctx)
        result: TriageResult = ctx.response
        assert result.routing == "self-serve"

    @pytest.mark.asyncio
    async def test_full_aper_loop(self) -> None:
        agent = TriageAgent(_mock_client())
        result = await agent.run("I was charged twice for my subscription (cust_001)")
        assert isinstance(result, TriageResult)
        assert result.category == "billing"
        assert result.customer_id == "cust_001"
        assert result.routing == "investigate"

    @pytest.mark.asyncio
    async def test_hooks_fire(self) -> None:
        fired: list[str] = []
        agent = TriageAgent(_mock_client())
        agent.on(HookPoint.PRE_ANALYZE, lambda ctx: fired.append("pre_analyze"))
        agent.on(HookPoint.POST_RESPOND, lambda ctx: fired.append("post_respond"))
        await agent.run("test ticket cust_001")
        assert "pre_analyze" in fired
        assert "post_respond" in fired


# ===================================================================
# InvestigatorAgent tests
# ===================================================================


class TestInvestigatorAgent:
    @pytest.mark.asyncio
    async def test_analyze_extracts_search_queries(self) -> None:
        triage = _sample_triage_result()
        agent = InvestigatorAgent(_mock_client())
        ctx = AgentContext(phase=AgentPhase.ANALYZE, input_data=triage)
        await agent.analyze(ctx)
        assert ctx.analysis["category"] == "billing"
        assert len(ctx.analysis["search_queries"]) > 0

    @pytest.mark.asyncio
    async def test_plan_builds_diagnostic_steps(self) -> None:
        triage = _sample_triage_result()
        agent = InvestigatorAgent(_mock_client())
        ctx = AgentContext(phase=AgentPhase.PLAN)
        ctx.analysis = {
            "triage": triage,
            "category": "billing",
            "severity": "P2",
            "customer_id": "cust_001",
            "search_queries": ["charged twice subscription"],
        }
        await agent.plan(ctx)
        assert len(ctx.plan["steps"]) >= 2

    @pytest.mark.asyncio
    async def test_execute_finds_similar_tickets(self) -> None:
        triage = _sample_triage_result()
        agent = InvestigatorAgent(_mock_client())
        ctx = AgentContext(phase=AgentPhase.EXECUTE)
        ctx.analysis = {
            "triage": triage,
            "category": "billing",
            "severity": "P2",
            "customer_id": "cust_001",
            "search_queries": ["charge"],
        }
        ctx.plan = {"steps": [], "queries": ["charge"]}
        await agent.execute(ctx)
        findings = ctx.results[0]
        assert len(findings["similar_tickets"]) >= 1
        assert findings["similar_tickets"][0]["ticket_id"] == "TKT-1001"

    @pytest.mark.asyncio
    async def test_execute_finds_kb_articles(self) -> None:
        triage = _sample_triage_result()
        agent = InvestigatorAgent(_mock_client())
        ctx = AgentContext(phase=AgentPhase.EXECUTE)
        ctx.analysis = {
            "triage": triage,
            "category": "billing",
            "severity": "P2",
            "customer_id": "cust_001",
            "search_queries": ["duplicate charge"],
        }
        ctx.plan = {"steps": [], "queries": ["duplicate charge"]}
        await agent.execute(ctx)
        findings = ctx.results[0]
        assert len(findings["kb_articles"]) >= 1

    @pytest.mark.asyncio
    async def test_full_aper_loop(self) -> None:
        triage = _sample_triage_result()
        agent = InvestigatorAgent(_mock_client())
        result = await agent.run(triage)
        assert isinstance(result, InvestigationResult)
        assert result.root_cause
        assert not result.should_escalate

    @pytest.mark.asyncio
    async def test_p1_triggers_escalation(self) -> None:
        triage = _sample_triage_result(severity="P1")
        agent = InvestigatorAgent(_mock_client())
        result = await agent.run(triage)
        assert isinstance(result, InvestigationResult)
        assert result.should_escalate is True


# ===================================================================
# ResolutionAgent tests
# ===================================================================


class TestResolutionAgent:
    @pytest.mark.asyncio
    async def test_analyze_determines_apology_for_billing(self) -> None:
        investigation = _sample_investigation_result()
        agent = ResolutionAgent(_mock_client())
        ctx = AgentContext(phase=AgentPhase.ANALYZE, input_data=investigation)
        await agent.analyze(ctx)
        assert ctx.analysis["resolution_type"] == "apology"

    @pytest.mark.asyncio
    async def test_analyze_determines_escalation(self) -> None:
        investigation = _sample_investigation_result(should_escalate=True)
        agent = ResolutionAgent(_mock_client())
        ctx = AgentContext(phase=AgentPhase.ANALYZE, input_data=investigation)
        await agent.analyze(ctx)
        assert ctx.analysis["resolution_type"] == "escalation"

    @pytest.mark.asyncio
    async def test_execute_drafts_response(self) -> None:
        investigation = _sample_investigation_result()
        agent = ResolutionAgent(_mock_client())
        ctx = AgentContext(phase=AgentPhase.EXECUTE)
        ctx.plan = {
            "resolution_type": "apology",
            "template": "Dear {details}",
            "investigation": investigation,
        }
        ctx.metadata = {}
        await agent.execute(ctx)
        assert ctx.results[0]["draft"]
        assert ctx.results[0]["internal_notes"]

    @pytest.mark.asyncio
    async def test_full_aper_loop(self) -> None:
        investigation = _sample_investigation_result()
        agent = ResolutionAgent(_mock_client())
        result = await agent.run(investigation)
        assert isinstance(result, ResolutionResult)
        assert result.response_type == "apology"
        assert "Acme Corp" in result.customer_message
        assert result.resolution_time_ms > 0

    @pytest.mark.asyncio
    async def test_compliance_hook_flags_pii(self) -> None:
        _sample_investigation_result()
        ResolutionAgent(_mock_client())
        ctx = AgentContext(phase=AgentPhase.RESPOND)
        ctx.metadata = {"response_draft": "Your card 4111-1111-1111-1111 was refunded"}
        ResolutionAgent._hook_pre_respond(ctx)
        assert "compliance_violations" in ctx.metadata
        assert any("credit card" in v for v in ctx.metadata["compliance_violations"])

    @pytest.mark.asyncio
    async def test_compliance_hook_flags_internal_jargon(self) -> None:
        _sample_investigation_result()
        ResolutionAgent(_mock_client())
        ctx = AgentContext(phase=AgentPhase.RESPOND)
        ctx.metadata = {"response_draft": "We updated your capability token to Tier B"}
        ResolutionAgent._hook_pre_respond(ctx)
        assert "compliance_violations" in ctx.metadata

    @pytest.mark.asyncio
    async def test_compliance_hook_passes_clean_response(self) -> None:
        ctx = AgentContext(phase=AgentPhase.RESPOND)
        ctx.metadata = {"response_draft": "We've issued a refund. It should appear in 3-5 days."}
        ResolutionAgent._hook_pre_respond(ctx)
        assert "compliance_violations" not in ctx.metadata


# ===================================================================
# Hook firing order
# ===================================================================


class TestHookOrder:
    @pytest.mark.asyncio
    async def test_triage_hooks_fire_in_order(self) -> None:
        fired: list[str] = []
        agent = TriageAgent(_mock_client())

        for hp in HookPoint:
            agent.on(hp, lambda ctx, name=hp.value: fired.append(name))

        await agent.run("billing issue cust_001")

        # Verify APER hook order
        aper_hooks = [h for h in fired if h.startswith("pre_") or h.startswith("post_")]
        expected_order = [
            "pre_analyze", "post_analyze",
            "pre_plan", "post_plan",
            "pre_execute", "post_execute",
            "pre_respond", "post_respond",
        ]
        assert aper_hooks == expected_order

    @pytest.mark.asyncio
    async def test_investigator_post_execute_hook_fires(self) -> None:
        fired: list[str] = []
        triage = _sample_triage_result()
        agent = InvestigatorAgent(_mock_client())
        agent.on(HookPoint.POST_EXECUTE, lambda ctx: fired.append("post_execute"))
        await agent.run(triage)
        assert "post_execute" in fired

    @pytest.mark.asyncio
    async def test_resolution_pre_respond_hook_fires(self) -> None:
        fired: list[str] = []
        investigation = _sample_investigation_result()
        agent = ResolutionAgent(_mock_client())
        agent.on(HookPoint.PRE_RESPOND, lambda ctx: fired.append("pre_respond"))
        await agent.run(investigation)
        assert "pre_respond" in fired


# ===================================================================
# Data structure serialization
# ===================================================================


class TestSerialization:
    def test_triage_result_to_dict(self) -> None:
        result = _sample_triage_result()
        d = result.to_dict()
        assert d["customer_id"] == "cust_001"
        assert d["category"] == "billing"
        assert d["routing"] == "investigate"

    def test_investigation_result_to_dict(self) -> None:
        result = _sample_investigation_result()
        d = result.to_dict()
        assert d["root_cause"]
        assert d["triage"]["customer_id"] == "cust_001"

    def test_resolution_result_to_dict(self) -> None:
        investigation = _sample_investigation_result()
        result = ResolutionResult(
            investigation=investigation,
            response_type="apology",
            customer_message="We're sorry...",
            internal_notes="Refund issued",
            resolution_time_ms=150.0,
        )
        d = result.to_dict()
        assert d["response_type"] == "apology"
        assert d["resolution_time_ms"] == 150.0
        assert d["investigation"]["triage"]["customer_id"] == "cust_001"
