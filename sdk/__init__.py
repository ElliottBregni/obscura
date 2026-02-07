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
from sdk.client import ObscuraClient

__all__ = [
    # Client
    "ObscuraClient",
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
