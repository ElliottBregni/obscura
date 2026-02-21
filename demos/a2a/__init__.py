"""
demos.a2a — Multi-agent support pipeline over the A2A protocol.

Three support agents (Triage, Investigator, Resolution) each run as
independent A2A servers. An orchestrator discovers them, invokes them
via ``A2AClient``, and pipes a support ticket through the pipeline
using standard A2A protocol calls.

Demonstrates:
    - A2A server creation with ``A2AService`` + transport routers
    - Agent discovery via ``/.well-known/agent.json``
    - Blocking ``message/send`` over JSON-RPC
    - Streaming ``message/stream`` over SSE
    - Tool adapter: ``register_remote_agent_as_tool``
"""

from demos.a2a.agents import (
    create_investigator_app,
    create_resolution_app,
    create_triage_app,
)
from demos.a2a.orchestrator import A2APipeline, A2AResult

__all__ = [
    "create_triage_app",
    "create_investigator_app",
    "create_resolution_app",
    "A2APipeline",
    "A2AResult",
]
