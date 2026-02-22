"""Agent subpackage: base agents, runtime, and loop utilities."""

from obscura.agent.agent import BaseAgent
from obscura.core.agent_loop import AgentLoop
from obscura.agent.agents import Agent, AgentRuntime, MCPConfig, AgentStatus
from obscura.agent.peers import AgentRef, PeerInvocationEnvelope, PeerRegistry

__all__ = [
    "BaseAgent",
    "Agent",
    "AgentRuntime",
    "MCPConfig",
    "AgentStatus",
    "AgentLoop",
    "AgentRef",
    "PeerRegistry",
    "PeerInvocationEnvelope",
]
