"""Local peer discovery and invocation models for agent-to-agent calls."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field
from obscura.integrations.a2a.client import A2AClient

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


class RemoteAgentRef(BaseModel):
    """Reference to a remote A2A peer endpoint/agent."""

    kind: Literal["a2a_remote"] = "a2a_remote"
    url: str
    name: str = ""
    status: str = "configured"
    capabilities: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    description: str = ""


def _default_local_refs() -> list[AgentRef]:
    return []


def _default_remote_refs() -> list[RemoteAgentRef]:
    return []


class PeerCatalog(BaseModel):
    """Unified catalog containing local and remote peers."""

    local: list[AgentRef] = Field(default_factory=_default_local_refs)
    remote: list[RemoteAgentRef] = Field(default_factory=_default_remote_refs)


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

    async def discover_remote(
        self,
        urls: list[str],
        *,
        auth_token: str | None = None,
        fetch_cards: bool = False,
    ) -> list[RemoteAgentRef]:
        """Return remote A2A peers from configured URLs (optionally via discover())."""
        refs: list[RemoteAgentRef] = []
        for url in urls:
            if not fetch_cards:
                refs.append(RemoteAgentRef(url=url))
                continue

            client = A2AClient(url, auth_token=auth_token)
            try:
                await client.connect()
                card = await client.discover()
                capability_names: list[str] = []
                if bool(getattr(card.capabilities, "streaming", False)):
                    capability_names.append("streaming")
                if bool(getattr(card.capabilities, "push_notifications", False)):
                    capability_names.append("push_notifications")
                if bool(getattr(card.capabilities, "extended_card", False)):
                    capability_names.append("extended_card")
                refs.append(
                    RemoteAgentRef(
                        url=url,
                        name=str(getattr(card, "name", "") or ""),
                        status="discovered",
                        capabilities=tuple(capability_names),
                        skills=tuple(skill.name for skill in card.skills),
                        description=str(getattr(card, "description", "") or ""),
                    )
                )
            except Exception:
                refs.append(RemoteAgentRef(url=url, status="error"))
            finally:
                try:
                    await client.disconnect()
                except Exception:
                    pass
        return refs
