"""Local peer discovery and invocation models for agent-to-agent calls."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from obscura.agent.agents import Agent, AgentRuntime


class AgentRef(BaseModel):
    """Reference to a peer agent."""

    kind: Literal["local"] = "local"
    runtime_id: str
    agent_id: str
    name: str
    model: str
    status: str
    capabilities: tuple[str, ...] = ()


class PeerInvocationEnvelope(BaseModel):
    """Correlation metadata attached to local peer invocations."""

    request_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    caller_agent_id: str = ""
    target_agent_id: str = ""
    mode: Literal["blocking", "streaming", "loop", "stream_loop"] = "blocking"


class PeerRegistry:
    """Registry that discovers and resolves local peers inside one runtime."""

    def __init__(self, runtime: AgentRuntime) -> None:
        self._runtime = runtime

    def discover(self) -> list[AgentRef]:
        """Return local peer references for all agents in this runtime."""
        refs: list[AgentRef] = []
        for agent in self._runtime.list_agents():
            refs.append(
                AgentRef(
                    runtime_id=self._runtime.runtime_id,
                    agent_id=agent.id,
                    name=agent.config.name,
                    model=agent.config.model,
                    status=agent.status.name,
                    capabilities=("local_invoke", "local_stream"),
                )
            )
        return refs

    def resolve(self, target: AgentRef | str) -> Agent | None:
        """Resolve a target AgentRef or agent_id string to a local Agent."""
        target_id = target.agent_id if isinstance(target, AgentRef) else str(target)
        return self._runtime.get_agent(target_id)

