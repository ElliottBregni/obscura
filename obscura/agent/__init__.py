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
from obscura.agent.interaction import (
    AttentionPriority,
    AttentionRequest,
    InteractionBus,
    UserResponse,
)
from obscura.agent.loop_agent import LoopAgent
from obscura.agent.daemon_agent import DaemonAgent
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
    # Long-running agents
    "LoopAgent",
    "DaemonAgent",
    "InteractionBus",
    "AttentionPriority",
    "AttentionRequest",
    "UserResponse",
]
