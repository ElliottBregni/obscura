"""
demos.support — Multi-agent customer support pipeline for Obscura.

Three-agent APER pipeline: Triage → Investigate → Resolve.
Demonstrates full hook surface, tool-calling, memory integration,
telemetry, and capability tier enforcement on the Copilot backend.
"""

from demos.support.agents import InvestigatorAgent, ResolutionAgent, TriageAgent
from demos.support.orchestrator import SupportPipeline, SupportResult
from demos.support.tools import (
    check_order_status,
    escalate_to_human,
    query_customer,
    search_knowledge_base,
    search_tickets,
    send_response,
)

__all__ = [
    "TriageAgent",
    "InvestigatorAgent",
    "ResolutionAgent",
    "SupportPipeline",
    "SupportResult",
    "search_tickets",
    "query_customer",
    "check_order_status",
    "search_knowledge_base",
    "escalate_to_human",
    "send_response",
]
