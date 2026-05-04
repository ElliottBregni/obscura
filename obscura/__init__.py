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

from typing import TYPE_CHECKING, Any

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

if TYPE_CHECKING:
    from obscura.agent.agent import BaseAgent
    from obscura.core.agent_loop import AgentLoop
    from obscura.core.agent_loop_factory import is_v2_enabled, make_agent_loop
    from obscura.core.agent_loop_v2 import AgentLoopV2, AgentLoopV2Config
    from obscura.core.auth import AuthConfig
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


def __getattr__(name: str) -> Any:
    return _resolve(name)


__all__ = [
    # Types and protocols
    "AgentContext",
    # Lazy: agent loops (v2 is the canonical default; AgentLoop is
    # deprecated — fires DeprecationWarning at construction)
    "AgentLoop",
    "AgentLoopV2",
    "AgentLoopV2Config",
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
    "is_v2_enabled",
    "make_agent_loop",
    "tool",
]
