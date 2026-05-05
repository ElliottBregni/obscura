"""Local peer discovery and invocation models for agent-to-agent calls.

The peer reference models (``AgentRef``, ``RemoteAgentRef``,
``UnixSocketAgentRef``) live canonically in
:mod:`obscura.core.models.peers` as a discriminated Pydantic union. This
module keeps the historical import paths working and houses the runtime
side of the peer system (``PeerRegistry``, ``PeerCatalog``,
``PeerInvocationEnvelope``).
"""

from __future__ import annotations

import contextlib
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from obscura.core.enums.agent import InvocationMode
from obscura.core.models.peers import (
    A2ARemoteAgentRef,
    AgentRef,
    AgentRefUnion,
    LocalAgentRef,
    RemoteAgentRef,
    UnixSocketAgentRef,
)
from obscura.integrations.a2a.client import A2AClient
import logging

logger = logging.getLogger(__name__)


if TYPE_CHECKING:
    from obscura.agent.agents import Agent, AgentRuntime


__all__ = [
    "A2ARemoteAgentRef",
    "AgentRef",
    "AgentRefUnion",
    "LocalAgentRef",
    "PeerCatalog",
    "PeerInvocationEnvelope",
    "PeerRegistry",
    "RemoteAgentRef",
    "UnixSocketAgentRef",
]


def _default_local_refs() -> list[AgentRef]:
    return []


def _default_remote_refs() -> list[RemoteAgentRef]:
    return []


def _default_unix_socket_refs() -> list[UnixSocketAgentRef]:
    return []


class PeerCatalog(BaseModel):
    """Unified catalog containing local, remote, and Unix socket peers."""

    local: list[AgentRef] = Field(default_factory=_default_local_refs)
    remote: list[RemoteAgentRef] = Field(default_factory=_default_remote_refs)
    unix_socket: list[UnixSocketAgentRef] = Field(
        default_factory=_default_unix_socket_refs,
    )


class PeerInvocationEnvelope(BaseModel):
    """Correlation metadata attached to local peer invocations."""

    request_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    caller_agent_id: str = ""
    target_agent_id: str = ""
    mode: InvocationMode = InvocationMode.BLOCKING


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
                ),
            )
        return refs

    def resolve(self, target: AgentRef | str) -> Agent | None:
        """Resolve a target AgentRef or agent_id string to a local Agent."""
        target_id = target.agent_id if isinstance(target, AgentRef) else str(target)
        return self._runtime.get_agent(target_id)

    async def discover_unix_socket(
        self,
        paths: list[str],
    ) -> list[UnixSocketAgentRef]:
        """Return Unix socket peer refs, checking that each socket exists."""
        import os

        refs: list[UnixSocketAgentRef] = []
        for path in paths:
            status = "available" if os.path.exists(path) else "unavailable"
            refs.append(
                UnixSocketAgentRef(
                    socket_path=path,
                    name=os.path.basename(path).removesuffix(".sock"),
                    status=status,
                ),
            )
        return refs

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
                    ),
                )
            except Exception:
                logger.debug("suppressed exception in discover_remote", exc_info=True)
                refs.append(RemoteAgentRef(url=url, status="error"))
            finally:
                with contextlib.suppress(Exception):
                    await client.disconnect()
        return refs
