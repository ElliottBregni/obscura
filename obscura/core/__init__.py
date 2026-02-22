"""
obscura.core — Stable runtime API surface.

This package contains the foundational types, tools, configuration, and
client that app developers import by default::

    from obscura.core import ObscuraClient, Message, Backend, tool
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
    # Auth
    "AuthConfig",
    "TokenRefresher",
    "resolve_auth",
    # Config
    "ObscuraConfig",
    # Context
    "ContextLoader",
    # Handlers
    "RequestHandler",
    "SimpleHandler",
    # Sessions
    "SessionStore",
    # Tools
    "ToolRegistry",
    "tool",
    # Types
    "AgentContext",
    "AgentPhase",
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
]
