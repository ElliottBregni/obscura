"""
sdk — Unified wrapper for GitHub Copilot SDK and Claude Agent SDK.

Public API::

    from sdk import ObscuraClient, Backend, Message, StreamChunk, tool, AuthConfig

    async with ObscuraClient("copilot", model_alias="copilot_automation_safe") as client:
        response = await client.send("explain this code")
        print(response.text)
"""

from __future__ import annotations

from sdk._auth import AuthConfig
from sdk._tools import ToolRegistry, tool
from sdk._types import (
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
from sdk.agent import BaseAgent
from sdk.client import ObscuraClient
from sdk.context import ContextLoader
from sdk.handlers import RequestHandler, SimpleHandler

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
    # Context
    "ContextLoader",
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
    # Tools
    "tool",
    "ToolRegistry",
]
