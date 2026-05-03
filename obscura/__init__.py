"""obscura — Unified wrapper for Copilot, Claude, OpenAI, and LocalLLM backends.

Public API::

    from obscura import ObscuraClient, Backend, Message, StreamChunk, tool

    async with ObscuraClient("copilot", model_alias="copilot_automation_safe") as client:
        response = await client.send("explain this code")
        print(response.text)

The eager surface here is small — protocols, types, the tool registry, and
the auth user model. None of it pulls SDK chains, so ``import obscura`` is
cheap. Heavier names (``ObscuraClient``, ``BaseAgent``, the OpenClaw bridge,
…) load on first attribute access via PEP 562 ``__getattr__``. The full lazy
registry lives in :mod:`obscura.lazy`.
"""

from __future__ import annotations

from typing import Any

# --- Eager: contracts, types, registries. Must NOT pull SDK chains. ---------
from obscura.auth.models import AuthenticatedUser
from obscura.core.tool_context import (
    ToolContext,
    bind_tool_context,
    current_tool_context,
)
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

# --- Lazy: heavier names accessed via PEP 562 __getattr__ -------------------
from obscura.lazy import MissingExtraError
from obscura.lazy import resolve as _resolve


def __getattr__(name: str) -> Any:
    return _resolve(name)


__all__ = [
    # Types and protocols
    "AgentContext",
    "AgentPhase",
    "AuthConfig",
    "AuthenticatedUser",
    "Backend",
    "BackendProtocol",
    # Lazy: agent
    "BaseAgent",
    # Lazy: openclaw bridge
    "BackendRoutingPolicy",
    "ChunkKind",
    "ContentBlock",
    "ContextLoader",
    "HookContext",
    "HookPoint",
    "MemoryWriteRequest",
    "Message",
    "MissingExtraError",
    # Lazy: client / config
    "ObscuraClient",
    "ObscuraConfig",
    "OpenClawBridge",
    "OpenClawBridgeConfig",
    "RequestHandler",
    "RequestMetadata",
    "Role",
    "RunAgentRequest",
    "SemanticSearchRequest",
    "SessionRef",
    "SimpleHandler",
    "SpawnAgentRequest",
    "StreamChunk",
    "ToolContext",
    "ToolRegistry",
    "ToolSpec",
    "WorkflowRunRequest",
    "bind_tool_context",
    "current_tool_context",
    "tool",
]
