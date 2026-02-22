"""Agent subpackage: base agents, runtime, and loop utilities."""

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
from obscura.agent.peers import (
    AgentRef,
    PeerCatalog,
    PeerInvocationEnvelope,
    PeerRegistry,
    RemoteAgentRef,
)

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
]
