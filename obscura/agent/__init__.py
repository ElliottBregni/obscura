"""Agent subpackage: base agents, runtime, and loop utilities."""

from __future__ import annotations

from typing import Any

from obscura.agent.agent import BaseAgent
from obscura.agent.agents import (
    Agent,
    AgentRuntime,
    AgentStatus,
    MCPConfig,
    RuntimeLifecycleEvent,
    RuntimeLifecycleHook,
)
from obscura.agent.aper_loop_agent import APERLoopAgent, APERMode
from obscura.agent.daemon_agent import DaemonAgent
from obscura.agent.interaction import (
    AttentionPriority,
    AttentionRequest,
    InteractionBus,
    UserResponse,
)
from obscura.agent.loop_agent import LoopAgent
from obscura.agent.peers import (
    AgentRef,
    PeerCatalog,
    PeerInvocationEnvelope,
    PeerRegistry,
    RemoteAgentRef,
)
from obscura.agent.supervised_runtime import SupervisedRuntime, SupervisedRuntimeConfig
from obscura.core.agent_loop_factory import make_agent_loop
from obscura.core.agent_loop_v2 import AgentLoopV2

# ---------------------------------------------------------------------------
# Agent type registry — maps spec ``agent_type`` values to concrete classes
# ---------------------------------------------------------------------------

AGENT_TYPE_REGISTRY: dict[str, type[Any]] = {
    "loop": LoopAgent,
    "daemon": DaemonAgent,
    "aper": APERLoopAgent,
}

__all__ = [
    # Registry
    "AGENT_TYPE_REGISTRY",
    "APERLoopAgent",
    "APERMode",
    "Agent",
    "AgentLoopV2",
    "AgentRef",
    "AgentRuntime",
    "AgentStatus",
    "AttentionPriority",
    "AttentionRequest",
    "BaseAgent",
    "DaemonAgent",
    "InteractionBus",
    # Long-running agents
    "LoopAgent",
    "MCPConfig",
    "PeerCatalog",
    "PeerInvocationEnvelope",
    "PeerRegistry",
    "RemoteAgentRef",
    "RuntimeLifecycleEvent",
    "RuntimeLifecycleHook",
    "SupervisedRuntime",
    "SupervisedRuntimeConfig",
    "UserResponse",
    "make_agent_loop",
]
