"""
obscura — Unified wrapper for Copilot, Claude, OpenAI, and LocalLLM backends.

Public API::

    from obscura import ObscuraClient, Backend, Message, StreamChunk, tool, AuthConfig

    async with ObscuraClient("copilot", model_alias="copilot_automation_safe") as client:
        response = await client.send("explain this code")
        print(response.text)
"""

from __future__ import annotations

from obscura.core.auth import AuthConfig
from obscura.core.tools import ToolRegistry, tool
from obscura.core.types import (
    AgentContext,
    AgentPhase,
    Backend,
    BackendProtocol,
    ChunkKind,
    ContentBlock,
    HookContext,
    HookPoint,
    Message,
    Role,
    SessionRef,
    StreamChunk,
    ToolSpec,
)
from obscura.agent.agent import BaseAgent
from obscura.auth.models import AuthenticatedUser
from obscura.core.client import ObscuraClient
from obscura.core.config import ObscuraConfig
from obscura.core.context import ContextLoader
from obscura.core.handlers import RequestHandler, SimpleHandler
from obscura.openclaw_bridge import (
    BackendRoutingPolicy,
    MemoryWriteRequest,
    OpenClawBridge,
    OpenClawBridgeConfig,
    RequestMetadata,
    RunAgentRequest,
    SemanticSearchRequest,
    SpawnAgentRequest,
    WorkflowRunRequest,
)

__all__ = [
    # Client
    "ObscuraClient",
    # Agent
    "BaseAgent",
    "AgentContext",
    "AgentPhase",
    # Handlers
    "RequestHandler",
    "SimpleHandler",
    # OpenClaw bridge
    "OpenClawBridge",
    "OpenClawBridgeConfig",
    "BackendRoutingPolicy",
    "RequestMetadata",
    "SpawnAgentRequest",
    "RunAgentRequest",
    "MemoryWriteRequest",
    "SemanticSearchRequest",
    "WorkflowRunRequest",
    # Context
    "ContextLoader",
    # Config
    "ObscuraConfig",
    # Types
    "Backend",
    "BackendProtocol",
    "ChunkKind",
    "ContentBlock",
    "HookContext",
    "HookPoint",
    "Message",
    "Role",
    "SessionRef",
    "StreamChunk",
    "ToolSpec",
    # Auth
    "AuthConfig",
    "AuthenticatedUser",
    # Tools
    "tool",
    "ToolRegistry",
]
