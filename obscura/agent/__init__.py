"""Agent subpackage: base agents, runtime, and loop utilities."""

from __future__ import annotations

from obscura.agent.agent import BaseAgent
from obscura.core.agent_loop import AgentLoop
from obscura.agent.agents import (
    Agent,
    AgentRuntime,
    MCPConfig,
    AgentStatus,
    RuntimeLifecycleEvent,
    RuntimeLifecycleHook,
)
from obscura.agent.interaction import (
    AttentionPriority,
    AttentionRequest,
    InteractionBus,
    UserResponse,
)
from obscura.agent.loop_agent import LoopAgent
from obscura.agent.daemon_agent import DaemonAgent
from obscura.agent.aper_loop_agent import APERLoopAgent
from obscura.agent.peers import (
    AgentRef,
    PeerCatalog,
    PeerInvocationEnvelope,
    PeerRegistry,
    RemoteAgentRef,
)

# ---------------------------------------------------------------------------
# Agent type registry — maps spec ``agent_type`` values to concrete classes
# ---------------------------------------------------------------------------

AGENT_TYPE_REGISTRY: dict[str, type[BaseAgent]] = {
    "loop": LoopAgent,
    "daemon": DaemonAgent,
    "aper": APERLoopAgent,
}

__all__ = [
    "BaseAgent",
    "Agent",
    "AgentRuntime",
    "MCPConfig",
    "AgentStatus",
    "AgentLoop",
    "RuntimeLifecycleEvent",
    "RuntimeLifecycleHook",
    "AgentRef",
    "RemoteAgentRef",
    "PeerCatalog",
    "PeerRegistry",
    "PeerInvocationEnvelope",
    # Long-running agents
    "LoopAgent",
    "DaemonAgent",
    "APERLoopAgent",
    "InteractionBus",
    "AttentionPriority",
    "AttentionRequest",
    "UserResponse",
    # Registry
    "AGENT_TYPE_REGISTRY",
]
