"""obscura.a2a — Agent-to-Agent (A2A) protocol integration for Obscura.

Provides both A2A Server (expose Obscura agents to external callers)
and A2A Client (invoke remote A2A agents from within Obscura).

Supports all protocol bindings: JSON-RPC 2.0, HTTP/REST, SSE, and gRPC.
Task state persisted in Redis for durability across restarts.
"""

from typing import TYPE_CHECKING

from obscura.core.enums.protocol import A2AMethod, A2ATaskState
from obscura.integrations.a2a.types import (
    A2AMessage,
    AgentCard,
    AgentSkill,
    Artifact,
    AuthScheme,
    DataPart,
    FileContent,
    FilePart,
    Part,
    SendMessageConfiguration,
    Task,
    TaskArtifactUpdateEvent,
    TaskStatus,
    TaskStatusUpdateEvent,
    TextPart,
)

if TYPE_CHECKING:
    from obscura.integrations.a2a.agent_card import AgentCardGenerator
    from obscura.integrations.a2a.client import A2AClient, A2ASessionManager
    from obscura.integrations.a2a.event_mapper import EventMapper
    from obscura.integrations.a2a.openclaw_bridge import OpenClawBridge, OpenClawBridgeConfig
    from obscura.integrations.a2a.server import ObscuraA2AServer
    from obscura.integrations.a2a.service import A2AService
    from obscura.integrations.a2a.store import (
        InMemoryTaskStore,
        RedisTaskStore,
        TaskStore,
    )

__all__ = [
    # Client
    "A2AClient",
    # Types
    "A2AMessage",
    "A2AMethod",
    # Service
    "A2AService",
    "A2ASessionManager",
    "A2ATaskState",
    "AgentCard",
    # Card generation
    "AgentCardGenerator",
    "AgentSkill",
    "Artifact",
    "AuthScheme",
    "DataPart",
    # Event mapping
    "EventMapper",
    "FileContent",
    "FilePart",
    # Store
    "InMemoryTaskStore",
    # OpenClaw bridge
    "OpenClawBridge",
    "OpenClawBridgeConfig",
    # Server
    "ObscuraA2AServer",
    "Part",
    "RedisTaskStore",
    "SendMessageConfiguration",
    "Task",
    "TaskArtifactUpdateEvent",
    "TaskStatus",
    "TaskStatusUpdateEvent",
    "TaskStore",
    "TextPart",
]


# Lazy imports to avoid pulling in everything on simple type imports
def __getattr__(name: str) -> object:
    if name == "A2AService":
        from obscura.integrations.a2a.service import A2AService

        return A2AService
    if name in ("InMemoryTaskStore", "RedisTaskStore", "TaskStore"):
        from obscura.integrations.a2a import store

        return getattr(store, name)
    if name in ("A2AClient", "A2ASessionManager"):
        from obscura.integrations.a2a import client

        return getattr(client, name)
    if name == "ObscuraA2AServer":
        from obscura.integrations.a2a.server import ObscuraA2AServer

        return ObscuraA2AServer
    if name == "AgentCardGenerator":
        from obscura.integrations.a2a.agent_card import AgentCardGenerator

        return AgentCardGenerator
    if name == "EventMapper":
        from obscura.integrations.a2a.event_mapper import EventMapper

        return EventMapper
    if name in ("OpenClawBridge", "OpenClawBridgeConfig"):
        from obscura.integrations.a2a import openclaw_bridge

        return getattr(openclaw_bridge, name)
    msg = f"module 'obscura.a2a' has no attribute {name!r}"
    raise AttributeError(msg)
