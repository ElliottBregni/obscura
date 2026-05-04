"""obscura.core — Stable runtime API surface.

This package contains the foundational types, tools, configuration, and
client that app developers import by default::

    from obscura.core import ObscuraClient, Message, Backend, tool

Kairos (the autonomous goal runtime) is intentionally NOT re-exported here —
it pulls heavy machinery and is properly accessed via ``obscura.core.kairos``
(goal/plan engine) or ``obscura.kairos`` (autonomous daemon). The eager
re-exports were removed in Stage A3 of the surface refactor; they were
unused by any caller.
"""

from __future__ import annotations

from obscura.core.auth import AuthConfig, TokenRefresher, resolve_auth
from obscura.core.config import ObscuraConfig
from obscura.core.context import ContextLoader
from obscura.core.handlers import RequestHandler, SimpleHandler
from obscura.core.sessions import SessionStore
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

__all__ = [
    "AgentContext",
    "AgentPhase",
    "AuthConfig",
    "Backend",
    "BackendProtocol",
    "ChunkKind",
    "ContentBlock",
    "ContextLoader",
    "HookContext",
    "HookPoint",
    "Message",
    "ObscuraConfig",
    "RequestHandler",
    "Role",
    "SessionRef",
    "SessionStore",
    "SimpleHandler",
    "StreamChunk",
    "TokenRefresher",
    "ToolRegistry",
    "ToolSpec",
    "resolve_auth",
    "tool",
]
