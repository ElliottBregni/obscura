"""Tests for demos.a2a — A2A multi-agent customer support pipeline.

Tests the full A2A demo surface:
    - Agent card discovery for all three agents
    - Triage agent: classify + enrich via A2A protocol
    - Investigator agent: search + root cause via A2A protocol
    - Resolution agent: draft response via A2A protocol
    - Full pipeline: ticket flows through all 3 agents
    - Streaming mode: SSE events from each agent
    - Tool adapter: agents registered and invoked as tools

All tests use ``ASGITransport`` — no network, no Redis, fully deterministic.
"""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from obscura.integrations.a2a.client import A2AClient
from obscura.integrations.a2a.tool_adapter import register_remote_agent_as_tool
from obscura.integrations.a2a.types import (
    TaskArtifactUpdateEvent,
    TaskState,
    TaskStatusUpdateEvent,
    TextPart,
)
from obscura.core.tools import ToolRegistry

from demos.a2a.agents import (
    create_investigator_app,
    create_resolution_app,
    create_triage_app,
)
from demos.a2a.orchestrator import A2APipeline
from demos.support.run import SAMPLE_TICKETS


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def triage_app() -> FastAPI:
    return create_triage_app()


@pytest.fixture
def investigator_app() -> FastAPI:
    return create_investigator_app()


@pytest.fixture
def resolution_app() -> FastAPI:
    return create_resolution_app()


@pytest.fixture
async def triage_client(triage_app: FastAPI):
    """A2AClient wired to triage agent via ASGI transport."""
    import httpx as _httpx

    transport = ASGITransport(app=triage_app)
    c = A2AClient("http://triage")
    c._http = _httpx.AsyncClient(
        transport=transport,
        base_url="http://triage",
        headers={"A2A-Version": "0.3"},
        timeout=30.0,
    )
    yield c
    await c.disconnect()


@pytest.fixture
async def investigator_client(investigator_app: FastAPI):
    import httpx as _httpx

    transport = ASGITransport(app=investigator_app)
    c = A2AClient("http://investigator")
    c._http = _httpx.AsyncClient(
        transport=transport,
        base_url="http://investigator",
        headers={"A2A-Version": "0.3"},
        timeout=30.0,
    )
    yield c
    await c.disconnect()


@pytest.fixture
async def resolution_client(resolution_app: FastAPI):
    import httpx as _httpx

    transport = ASGITransport(app=resolution_app)
    c = A2AClient("http://resolution")
    c._http = _httpx.AsyncClient(
        transport=transport,
        base_url="http://resolution",
        headers={"A2A-Version": "0.3"},
        timeout=30.0,
    )
    yield c
    await c.disconnect()


@pytest.fixture
def pipeline(
    triage_app: FastAPI,
    investigator_app: FastAPI,
    resolution_app: FastAPI,
) -> A2APipeline:
    return A2APipeline(
        triage_transport=ASGITransport(app=triage_app),
        investigator_transport=ASGITransport(app=investigator_app),
        resolution_transport=ASGITransport(app=resolution_app),
    )


# ---------------------------------------------------------------------------
# Agent Card Discovery
# ---------------------------------------------------------------------------


class TestAgentCardDiscovery:
    """Test that each agent publishes a valid Agent Card."""

    @pytest.mark.asyncio
    async def test_triage_card(self, triage_client: A2AClient) -> None:
        card = await triage_client.discover()
        assert card.name == "TriageAgent"
        assert card.protocolVersion == "0.3"
        assert len(card.skills) == 3
        skill_ids = {s.id for s in card.skills}
        assert skill_ids == {"classify", "extract_customer", "detect_urgency"}

    @pytest.mark.asyncio
    async def test_investigator_card(self, investigator_client: A2AClient) -> None:
        card = await investigator_client.discover()
        assert card.name == "InvestigatorAgent"
        assert len(card.skills) == 3
        skill_ids = {s.id for s in card.skills}
        assert skill_ids == {"search_similar", "search_kb", "root_cause"}

    @pytest.mark.asyncio
    async def test_resolution_card(self, resolution_client: A2AClient) -> None:
        card = await resolution_client.discover()
        assert card.name == "ResolutionAgent"
        assert len(card.skills) == 2
        skill_ids = {s.id for s in card.skills}
        assert skill_ids == {"draft_response", "send_response"}

    @pytest.mark.asyncio
    async def test_cards_have_streaming_capability(
        self,
        triage_client: A2AClient,
    ) -> None:
        card = await triage_client.discover()
        assert card.capabilities.streaming is True


# ---------------------------------------------------------------------------
# Triage Agent
# ---------------------------------------------------------------------------


class TestTriageAgent:
    """Test triage agent via A2A protocol."""

    @pytest.mark.asyncio
    async def test_billing_ticket(self, triage_client: A2AClient) -> None:
        task = await triage_client.send_message(SAMPLE_TICKETS["billing"])
        assert task.status.state == TaskState.COMPLETED
        assert len(task.artifacts) >= 1

        # Parse artifact
        text = _artifact_text(task)
        data = json.loads(text)
        assert data["category"] == "billing"
        assert data["severity"] == "P2"
        assert data["customer_id"] == "cust_001"
        assert data["routing"] == "investigate"

    @pytest.mark.asyncio
    async def test_urgent_ticket(self, triage_client: A2AClient) -> None:
        task = await triage_client.send_message(SAMPLE_TICKETS["urgent"])
        data = json.loads(_artifact_text(task))
        assert data["urgency_detected"] is True
        assert data["severity"] in ("P1", "P2")

    @pytest.mark.asyncio
    async def test_technical_ticket(self, triage_client: A2AClient) -> None:
        task = await triage_client.send_message(SAMPLE_TICKETS["technical"])
        data = json.loads(_artifact_text(task))
        assert data["category"] == "technical"
        assert data["customer_id"] == "cust_003"

    @pytest.mark.asyncio
    async def test_customer_enrichment(self, triage_client: A2AClient) -> None:
        task = await triage_client.send_message(SAMPLE_TICKETS["billing"])
        data = json.loads(_artifact_text(task))
        assert data["customer_info"] is not None
        assert data["customer_info"]["name"] == "Acme Corp"
        assert data["order_info"] is not None

    @pytest.mark.asyncio
    async def test_no_customer_id(self, triage_client: A2AClient) -> None:
        task = await triage_client.send_message("Generic question about pricing")
        data = json.loads(_artifact_text(task))
        assert data["customer_id"] == ""
        assert data["customer_info"] is None

    @pytest.mark.asyncio
    async def test_task_has_history(self, triage_client: A2AClient) -> None:
        task = await triage_client.send_message("Test history")
        assert len(task.history) >= 1
        assert task.history[0].role == "user"


# ---------------------------------------------------------------------------
# Investigator Agent
# ---------------------------------------------------------------------------


class TestInvestigatorAgent:
    """Test investigator agent via A2A protocol."""

    @pytest.mark.asyncio
    async def test_billing_investigation(self, investigator_client: A2AClient) -> None:
        # Simulate triage output as input
        triage_input = json.dumps(
            {
                "customer_id": "cust_001",
                "category": "billing",
                "severity": "P2",
                "urgency_detected": False,
                "customer_info": {"name": "Acme Corp", "plan": "enterprise"},
                "order_info": None,
                "original_ticket": "I was charged twice (cust_001)",
                "routing": "investigate",
            }
        )
        task = await investigator_client.send_message(triage_input)
        assert task.status.state == TaskState.COMPLETED

        data = json.loads(_artifact_text(task))
        assert "root_cause" in data
        assert "similar_tickets" in data
        assert "kb_articles" in data
        assert len(data["similar_tickets"]) > 0  # cust_001 has billing tickets
        assert len(data["kb_articles"]) > 0

    @pytest.mark.asyncio
    async def test_investigation_finds_kb(
        self,
        investigator_client: A2AClient,
    ) -> None:
        triage_input = json.dumps(
            {
                "customer_id": "cust_002",
                "category": "account",
                "severity": "P2",
                "urgency_detected": False,
                "customer_info": None,
                "order_info": None,
                "original_ticket": "Can't login after password reset (cust_002)",
                "routing": "investigate",
            }
        )
        task = await investigator_client.send_message(triage_input)
        data = json.loads(_artifact_text(task))
        assert len(data["kb_articles"]) > 0


# ---------------------------------------------------------------------------
# Resolution Agent
# ---------------------------------------------------------------------------


class TestResolutionAgent:
    """Test resolution agent via A2A protocol."""

    @pytest.mark.asyncio
    async def test_apology_response(self, resolution_client: A2AClient) -> None:
        inv_input = json.dumps(
            {
                "triage": {
                    "customer_id": "cust_001",
                    "category": "billing",
                    "severity": "P2",
                    "urgency_detected": False,
                    "customer_info": {"name": "Acme Corp", "plan": "enterprise"},
                    "order_info": None,
                    "original_ticket": "Double charge",
                    "routing": "investigate",
                },
                "similar_tickets": [
                    {"ticket_id": "TKT-1001", "resolution": "Refund issued"}
                ],
                "kb_articles": [
                    {"id": "kb_001", "title": "Handling Duplicate Charges"}
                ],
                "root_cause": "Duplicate charge from payment gateway retry",
                "recommended_action": "Issue refund",
                "should_escalate": False,
                "escalation_reason": None,
            }
        )
        task = await resolution_client.send_message(inv_input)
        assert task.status.state == TaskState.COMPLETED

        data = json.loads(_artifact_text(task))
        assert data["response_type"] == "apology"
        assert "Acme Corp" in data["customer_message"]
        assert "apologize" in data["customer_message"].lower()

    @pytest.mark.asyncio
    async def test_escalation_response(
        self,
        resolution_client: A2AClient,
    ) -> None:
        inv_input = json.dumps(
            {
                "triage": {
                    "customer_id": "cust_003",
                    "category": "technical",
                    "severity": "P1",
                    "urgency_detected": True,
                    "customer_info": {"name": "DataFlow Inc"},
                    "order_info": None,
                    "original_ticket": "Production down!",
                    "routing": "escalate",
                },
                "similar_tickets": [],
                "kb_articles": [],
                "root_cause": "Critical outage",
                "recommended_action": "Immediate investigation required",
                "should_escalate": True,
                "escalation_reason": "P1 severity",
            }
        )
        task = await resolution_client.send_message(inv_input)
        data = json.loads(_artifact_text(task))
        assert data["response_type"] == "escalation"
        assert "escalated" in data["customer_message"].lower()


# ---------------------------------------------------------------------------
# Full Pipeline (Blocking)
# ---------------------------------------------------------------------------


class TestFullPipeline:
    """Test the complete pipeline: Triage → Investigate → Resolve."""

    @pytest.mark.asyncio
    async def test_billing_pipeline(self, pipeline: A2APipeline) -> None:
        result = await pipeline.run(SAMPLE_TICKETS["billing"])

        assert result.mode == "blocking"
        assert "triage" in result.phases
        assert "investigation" in result.phases
        assert "resolution" in result.phases
        assert result.total_time_ms > 0

        # Triage produced valid JSON
        assert result.triage_json is not None
        assert result.triage_json["category"] == "billing"
        assert result.triage_json["customer_id"] == "cust_001"

        # Investigation found evidence
        assert result.investigation_json is not None
        assert len(result.investigation_json.get("similar_tickets", [])) > 0

        # Resolution drafted a response
        assert result.resolution_json is not None
        assert result.resolution_json["response_type"] in ("apology", "fix", "info")
        assert len(result.customer_message) > 0

    @pytest.mark.asyncio
    async def test_technical_pipeline(self, pipeline: A2APipeline) -> None:
        result = await pipeline.run(SAMPLE_TICKETS["technical"])
        assert "triage" in result.phases
        assert result.triage_json is not None
        assert result.triage_json["category"] == "technical"

    @pytest.mark.asyncio
    async def test_account_pipeline(self, pipeline: A2APipeline) -> None:
        result = await pipeline.run(SAMPLE_TICKETS["account"])
        assert "triage" in result.phases
        assert result.triage_json is not None
        assert result.triage_json["category"] == "account"

    @pytest.mark.asyncio
    async def test_pipeline_discovers_all_cards(
        self,
        pipeline: A2APipeline,
    ) -> None:
        result = await pipeline.run(SAMPLE_TICKETS["general"])
        assert "triage" in result.agent_cards
        assert "investigator" in result.agent_cards
        assert "resolution" in result.agent_cards

    @pytest.mark.asyncio
    async def test_all_tasks_completed(self, pipeline: A2APipeline) -> None:
        result = await pipeline.run(SAMPLE_TICKETS["billing"])
        assert result.triage_task is not None
        assert result.triage_task.status.state == TaskState.COMPLETED
        assert result.investigator_task is not None
        assert result.investigator_task.status.state == TaskState.COMPLETED
        assert result.resolution_task is not None
        assert result.resolution_task.status.state == TaskState.COMPLETED


# ---------------------------------------------------------------------------
# Streaming Mode
# ---------------------------------------------------------------------------


class TestStreamingPipeline:
    """Test the SSE streaming pipeline."""

    @pytest.mark.asyncio
    async def test_streaming_yields_events(self, pipeline: A2APipeline) -> None:
        events: list[tuple[str, object]] = []
        async for agent_name, event in pipeline.run_streaming(
            SAMPLE_TICKETS["billing"]
        ):
            events.append((agent_name, event))

        assert len(events) > 0

        # Should have events from all three agents
        agent_names = {name for name, _ in events}
        assert "triage" in agent_names
        assert "investigator" in agent_names
        assert "resolution" in agent_names

    @pytest.mark.asyncio
    async def test_streaming_has_status_and_artifacts(
        self,
        pipeline: A2APipeline,
    ) -> None:
        status_events: list[TaskStatusUpdateEvent] = []
        artifact_events: list[TaskArtifactUpdateEvent] = []
        async for _, event in pipeline.run_streaming(SAMPLE_TICKETS["billing"]):
            if isinstance(event, TaskStatusUpdateEvent):
                status_events.append(event)
            else:
                artifact_events.append(event)

        assert len(status_events) > 0
        assert len(artifact_events) > 0

    @pytest.mark.asyncio
    async def test_streaming_ends_with_completed(
        self,
        pipeline: A2APipeline,
    ) -> None:
        last_status: TaskStatusUpdateEvent | None = None
        async for _, event in pipeline.run_streaming(SAMPLE_TICKETS["billing"]):
            if isinstance(event, TaskStatusUpdateEvent):
                last_status = event

        assert last_status is not None
        assert last_status.status.state == TaskState.COMPLETED
        assert last_status.final is True


# ---------------------------------------------------------------------------
# Tool Adapter Mode
# ---------------------------------------------------------------------------


class TestToolAdapterPipeline:
    """Test agents invoked through the tool adapter interface."""

    @pytest.mark.asyncio
    async def test_tool_adapter_pipeline(self, pipeline: A2APipeline) -> None:
        result = await pipeline.run_tool_adapter(SAMPLE_TICKETS["billing"])

        assert result.mode == "tool-adapter"
        assert "triage" in result.phases
        assert "investigation" in result.phases
        assert "resolution" in result.phases

        assert result.triage_json is not None
        assert result.resolution_json is not None

    @pytest.mark.asyncio
    async def test_tool_registration(
        self,
        triage_app: FastAPI,
    ) -> None:
        """Test that an agent can be registered as a tool."""
        import httpx as _httpx

        transport = ASGITransport(app=triage_app)
        client = A2AClient("http://triage")
        client._http = _httpx.AsyncClient(
            transport=transport,
            base_url="http://triage",
            headers={"A2A-Version": "0.3"},
            timeout=30.0,
        )

        try:
            await client.discover()
            registry = ToolRegistry()
            spec = register_remote_agent_as_tool(
                registry,
                client,
                tool_name="triage_agent",
            )
            assert spec.name == "triage_agent"
            assert registry.get("triage_agent") is not None

            # Invoke via tool handler
            result = await spec.handler(message="Test ticket (cust_001)")
            assert len(result) > 0
            data = json.loads(result)
            assert "category" in data
        finally:
            await client.disconnect()


# ---------------------------------------------------------------------------
# Edge Cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Test edge cases and unusual inputs."""

    @pytest.mark.asyncio
    async def test_empty_ticket(self, pipeline: A2APipeline) -> None:
        result = await pipeline.run("")
        assert "triage" in result.phases
        # Should still produce valid JSON
        assert result.triage_json is not None

    @pytest.mark.asyncio
    async def test_unknown_customer(self, triage_client: A2AClient) -> None:
        task = await triage_client.send_message("Problem with cust_999")
        data = json.loads(_artifact_text(task))
        assert data["customer_id"] == "cust_999"
        assert data["customer_info"] is None

    @pytest.mark.asyncio
    async def test_all_sample_tickets(self, pipeline: A2APipeline) -> None:
        """Ensure every sample ticket runs without error."""
        for name, ticket in SAMPLE_TICKETS.items():
            result = await pipeline.run(ticket)
            assert len(result.phases) >= 1, f"Failed for sample: {name}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _artifact_text(task: object) -> str:
    """Extract all text from a task's artifacts."""
    parts: list[str] = []
    for artifact in task.artifacts:  # type: ignore[attr-defined]
        for part in artifact.parts:  # type: ignore[union-attr]
            if isinstance(part, TextPart):
                parts.append(part.text)
    return "".join(parts)
