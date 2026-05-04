"""Discriminated Pydantic union for peer-agent references.

Three flavors of peer reference share the wire-level ``kind`` field:
local agents inside this runtime, remote A2A endpoints, and Unix-socket
agents on the same host. The ``AgentRef`` discriminated union below
replaces the loose ``BaseModel`` triple in ``agent/peers.py`` while
preserving every legacy import name.

Wire format byte-identical with the previous models — the discriminator
strings come from ``PeerKind`` whose values match the strings already
serialized to JSON today (``"local"``, ``"a2a_remote"``, ``"unix_socket"``).
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from obscura.core.enums.agent import PeerKind


_PEER_CONFIG = ConfigDict(
    frozen=True,
    extra="forbid",
    validate_assignment=True,
    use_enum_values=False,
)


class LocalAgentRef(BaseModel):
    """Reference to an agent inside the current runtime."""

    model_config = _PEER_CONFIG

    kind: Literal[PeerKind.LOCAL] = PeerKind.LOCAL
    runtime_id: str
    agent_id: str
    name: str
    model: str
    status: str
    capabilities: tuple[str, ...] = ()


class A2ARemoteAgentRef(BaseModel):
    """Reference to a remote A2A peer endpoint/agent."""

    model_config = _PEER_CONFIG

    kind: Literal[PeerKind.A2A_REMOTE] = PeerKind.A2A_REMOTE
    url: str
    name: str = ""
    status: str = "configured"
    capabilities: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    description: str = ""


class UnixSocketAgentRef(BaseModel):
    """Reference to an agent reachable via Unix domain socket."""

    model_config = _PEER_CONFIG

    kind: Literal[PeerKind.UNIX_SOCKET] = PeerKind.UNIX_SOCKET
    socket_path: str
    name: str = ""
    status: str = "configured"
    capabilities: tuple[str, ...] = ()
    description: str = ""


AgentRefUnion = Annotated[
    LocalAgentRef | A2ARemoteAgentRef | UnixSocketAgentRef,
    Field(discriminator="kind"),
]
"""Discriminated union over the three peer reference variants."""


# Legacy names kept for back-compat:
AgentRef = LocalAgentRef
RemoteAgentRef = A2ARemoteAgentRef


__all__ = [
    "A2ARemoteAgentRef",
    "AgentRef",
    "AgentRefUnion",
    "LocalAgentRef",
    "RemoteAgentRef",
    "UnixSocketAgentRef",
]
