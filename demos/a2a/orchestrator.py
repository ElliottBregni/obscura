"""
demos.a2a.orchestrator — A2A pipeline: discover + invoke 3 agents via protocol.

``A2APipeline`` connects to three A2A agent servers (Triage, Investigator,
Resolution), discovers their agent cards, and pipes a support ticket through
the pipeline using standard A2A protocol calls.

Supports three execution modes:
    - **blocking**: JSON-RPC ``message/send`` with ``blocking=True``
    - **streaming**: SSE ``message/stream`` for real-time events
    - **tool-adapter**: ``register_remote_agent_as_tool`` wraps agents as tools

Usage::

    pipeline = A2APipeline(
        triage_transport=ASGITransport(app=triage_app),
        investigator_transport=ASGITransport(app=investigator_app),
        resolution_transport=ASGITransport(app=resolution_app),
    )
    result = await pipeline.run("I was charged twice (cust_001)")
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, AsyncIterator

import httpx

from sdk.a2a.client import A2AClient
from sdk.a2a.tool_adapter import register_remote_agent_as_tool
from sdk.a2a.types import (
    AgentCard,
    StreamEvent,
    Task,
    TaskArtifactUpdateEvent,
    TaskState,
    TaskStatusUpdateEvent,
)
from sdk.internal.tools import ToolRegistry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pipeline result
# ---------------------------------------------------------------------------


@dataclass
class A2AResult:
    """Complete pipeline result with A2A protocol audit trail."""

    ticket: str
    triage_task: Task | None = None
    investigator_task: Task | None = None
    resolution_task: Task | None = None
    triage_json: dict[str, Any] | None = None
    investigation_json: dict[str, Any] | None = None
    resolution_json: dict[str, Any] | None = None
    agent_cards: dict[str, AgentCard] = field(default_factory=dict)
    total_time_ms: float = 0.0
    phases: list[str] = field(default_factory=list)
    mode: str = "blocking"
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    @property
    def customer_message(self) -> str:
        if self.resolution_json:
            return self.resolution_json.get("customer_message", "")
        return "Pipeline did not complete to resolution."


# ---------------------------------------------------------------------------
# A2A Pipeline
# ---------------------------------------------------------------------------


class A2APipeline:
    """Orchestrates 3 A2A agents via protocol calls.

    Parameters
    ----------
    triage_transport:
        ``httpx.AsyncBaseTransport`` for the triage agent (e.g. ``ASGITransport``).
    investigator_transport:
        Transport for the investigator agent.
    resolution_transport:
        Transport for the resolution agent.
    """

    def __init__(
        self,
        triage_transport: httpx.AsyncBaseTransport,
        investigator_transport: httpx.AsyncBaseTransport,
        resolution_transport: httpx.AsyncBaseTransport,
    ) -> None:
        self._transports = {
            "triage": ("http://triage", triage_transport),
            "investigator": ("http://investigator", investigator_transport),
            "resolution": ("http://resolution", resolution_transport),
        }
        self._clients: dict[str, A2AClient] = {}

    async def connect(self) -> dict[str, AgentCard]:
        """Connect to all agents and discover their agent cards."""
        cards: dict[str, AgentCard] = {}
        for name, (url, transport) in self._transports.items():
            client = A2AClient(url)
            client._http = httpx.AsyncClient(
                transport=transport,
                base_url=url,
                headers={"A2A-Version": "0.3"},
                timeout=30.0,
            )
            self._clients[name] = client
            card = await client.discover()
            cards[name] = card
            logger.info("Discovered %s: %s (%d skills)", name, card.name, len(card.skills))
        return cards

    async def disconnect(self) -> None:
        """Disconnect all clients."""
        for client in self._clients.values():
            await client.disconnect()
        self._clients.clear()

    async def __aenter__(self) -> A2APipeline:
        await self.connect()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.disconnect()

    # ------------------------------------------------------------------
    # Blocking mode
    # ------------------------------------------------------------------

    async def run(self, ticket: str) -> A2AResult:
        """Run the full pipeline in blocking mode.

        Sends the ticket through Triage → Investigator → Resolution
        using ``message/send`` (JSON-RPC, blocking=True).
        """
        start = time.monotonic()
        cards = await self.connect()

        result = A2AResult(ticket=ticket, agent_cards=cards, mode="blocking")

        try:
            # Phase 1: Triage
            triage_client = self._clients["triage"]
            triage_task = await triage_client.send_message(ticket, blocking=True)
            result.triage_task = triage_task
            result.phases.append("triage")

            triage_text = _extract_artifact_text(triage_task)
            try:
                result.triage_json = json.loads(triage_text)
            except json.JSONDecodeError:
                result.triage_json = {"raw": triage_text}

            logger.info(
                "Triage complete: task=%s category=%s",
                triage_task.id,
                result.triage_json.get("category", "?"),
            )

            # Phase 2: Investigator — send triage JSON as input
            inv_client = self._clients["investigator"]
            inv_task = await inv_client.send_message(triage_text, blocking=True)
            result.investigator_task = inv_task
            result.phases.append("investigation")

            inv_text = _extract_artifact_text(inv_task)
            try:
                result.investigation_json = json.loads(inv_text)
            except json.JSONDecodeError:
                result.investigation_json = {"raw": inv_text}

            logger.info(
                "Investigation complete: task=%s root_cause=%s",
                inv_task.id,
                str(result.investigation_json.get("root_cause", "?"))[:60],
            )

            # Phase 3: Resolution — send investigation JSON as input
            res_client = self._clients["resolution"]
            res_task = await res_client.send_message(inv_text, blocking=True)
            result.resolution_task = res_task
            result.phases.append("resolution")

            res_text = _extract_artifact_text(res_task)
            try:
                result.resolution_json = json.loads(res_text)
            except json.JSONDecodeError:
                result.resolution_json = {"raw": res_text}

            logger.info("Resolution complete: task=%s", res_task.id)

        finally:
            result.total_time_ms = (time.monotonic() - start) * 1000
            await self.disconnect()

        return result

    # ------------------------------------------------------------------
    # Streaming mode
    # ------------------------------------------------------------------

    async def run_streaming(
        self, ticket: str,
    ) -> AsyncIterator[tuple[str, StreamEvent]]:
        """Run the pipeline in streaming mode.

        Yields ``(agent_name, event)`` tuples as each agent processes.
        Uses ``message/stream`` (SSE) for real-time output.
        """
        cards = await self.connect()

        try:
            # Phase 1: Triage (streaming)
            triage_text = ""
            triage_client = self._clients["triage"]
            async for event in triage_client.stream_message(ticket):
                yield ("triage", event)
                if isinstance(event, TaskArtifactUpdateEvent):
                    for part in event.artifact.parts:
                        if hasattr(part, "text"):
                            triage_text += part.text

            # Phase 2: Investigator (streaming)
            inv_text = ""
            inv_input = triage_text or ticket
            inv_client = self._clients["investigator"]
            async for event in inv_client.stream_message(inv_input):
                yield ("investigator", event)
                if isinstance(event, TaskArtifactUpdateEvent):
                    for part in event.artifact.parts:
                        if hasattr(part, "text"):
                            inv_text += part.text

            # Phase 3: Resolution (streaming)
            res_input = inv_text or inv_input
            res_client = self._clients["resolution"]
            async for event in res_client.stream_message(res_input):
                yield ("resolution", event)

        finally:
            await self.disconnect()

    # ------------------------------------------------------------------
    # Tool adapter mode
    # ------------------------------------------------------------------

    async def run_tool_adapter(self, ticket: str) -> A2AResult:
        """Run the pipeline using tool adapters.

        Registers all 3 agents as tools in a ``ToolRegistry``, then
        invokes them sequentially through the tool interface.
        """
        start = time.monotonic()
        cards = await self.connect()

        registry = ToolRegistry()
        result = A2AResult(ticket=ticket, agent_cards=cards, mode="tool-adapter")

        # Register all agents as tools
        for name, client in self._clients.items():
            await client.discover()
            register_remote_agent_as_tool(
                registry,
                client,
                tool_name=name,
            )

        try:
            # Invoke triage tool
            triage_tool = registry.get("triage")
            assert triage_tool is not None
            triage_text = await triage_tool.handler(message=ticket)
            result.phases.append("triage")
            try:
                result.triage_json = json.loads(triage_text)
            except json.JSONDecodeError:
                result.triage_json = {"raw": triage_text}

            # Invoke investigator tool
            inv_tool = registry.get("investigator")
            assert inv_tool is not None
            inv_text = await inv_tool.handler(message=triage_text)
            result.phases.append("investigation")
            try:
                result.investigation_json = json.loads(inv_text)
            except json.JSONDecodeError:
                result.investigation_json = {"raw": inv_text}

            # Invoke resolution tool
            res_tool = registry.get("resolution")
            assert res_tool is not None
            res_text = await res_tool.handler(message=inv_text)
            result.phases.append("resolution")
            try:
                result.resolution_json = json.loads(res_text)
            except json.JSONDecodeError:
                result.resolution_json = {"raw": res_text}

        finally:
            result.total_time_ms = (time.monotonic() - start) * 1000
            await self.disconnect()

        return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_artifact_text(task: Task) -> str:
    """Extract all text from a task's artifacts."""
    parts: list[str] = []
    for artifact in task.artifacts:
        for part in artifact.parts:
            if hasattr(part, "text"):
                parts.append(part.text)
    return "".join(parts) if parts else ""
