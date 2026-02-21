"""
sdk — Unified wrapper for Copilot, Claude, OpenAI, and LocalLLM backends.

Public API::

    from sdk import ObscuraClient, Backend, Message, StreamChunk, tool, AuthConfig

    async with ObscuraClient("copilot", model_alias="copilot_automation_safe") as client:
        response = await client.send("explain this code")
        print(response.text)
"""

from __future__ import annotations

from sdk.internal.auth import AuthConfig
from sdk.internal.tools import ToolRegistry, tool
from sdk.internal.types import (
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
from sdk.agent.agent import BaseAgent
from sdk.auth.models import AuthenticatedUser
from sdk.client import ObscuraClient
from sdk.config import ObscuraConfig
from sdk.context import ContextLoader
from sdk.handlers import RequestHandler, SimpleHandler
from sdk.openclaw_bridge import (
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
