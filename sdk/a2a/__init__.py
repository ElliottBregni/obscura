"""
sdk.a2a — Agent-to-Agent (A2A) protocol integration for Obscura.

Provides both A2A Server (expose Obscura agents to external callers)
and A2A Client (invoke remote A2A agents from within Obscura).

Supports all protocol bindings: JSON-RPC 2.0, HTTP/REST, SSE, and gRPC.
Task state persisted in Redis for durability across restarts.
"""

from sdk.a2a.types import (
    A2AMessage,
    A2AMethod,
    AgentCard,
    AgentSkill,
    Artifact,
    AuthScheme,
    DataPart,
    FilePart,
    FileContent,
    Part,
    SendMessageConfiguration,
    Task,
    TaskArtifactUpdateEvent,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
    TextPart,
)

__all__ = [
    # Types
    "A2AMessage",
    "A2AMethod",
    "AgentCard",
    "AgentSkill",
    "Artifact",
    "AuthScheme",
    "DataPart",
    "FilePart",
    "FileContent",
    "Part",
    "SendMessageConfiguration",
    "Task",
    "TaskArtifactUpdateEvent",
    "TaskState",
    "TaskStatus",
    "TaskStatusUpdateEvent",
    "TextPart",
    # Service
    "A2AService",
    # Store
    "InMemoryTaskStore",
    "RedisTaskStore",
    "TaskStore",
    # Client
    "A2AClient",
    "A2ASessionManager",
    # Server
    "ObscuraA2AServer",
    # Card generation
    "AgentCardGenerator",
    # Event mapping
    "EventMapper",
]

# Lazy imports to avoid pulling in everything on simple type imports
def __getattr__(name: str):
    if name == "A2AService":
        from sdk.a2a.service import A2AService
        return A2AService
    if name in ("InMemoryTaskStore", "RedisTaskStore", "TaskStore"):
        from sdk.a2a import store
        return getattr(store, name)
    if name in ("A2AClient", "A2ASessionManager"):
        from sdk.a2a import client
        return getattr(client, name)
    if name == "ObscuraA2AServer":
        from sdk.a2a.server import ObscuraA2AServer
        return ObscuraA2AServer
    if name == "AgentCardGenerator":
        from sdk.a2a.agent_card import AgentCardGenerator
        return AgentCardGenerator
    if name == "EventMapper":
        from sdk.a2a.event_mapper import EventMapper
        return EventMapper
    raise AttributeError(f"module 'sdk.a2a' has no attribute {name!r}")
