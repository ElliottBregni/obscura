"""obscura.core — Stable runtime API surface.

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
from obscura.core.kairos import (
    Kairos,
    KairosConfig,
    Goal,
    GoalBudget,
    GoalStatus,
    Plan,
    PlanStatus,
    Task,
    TaskStatus,
    TaskResult,
    Checkpoint,
    CheckpointKind,
    Intervention,
    InterventionKind,
    KairosEvent,
    KairosEventKind,
)

__all__ = [
    # Types
    "AgentContext",
    "AgentPhase",
    # Auth
    "AuthConfig",
    "Backend",
    "BackendProtocol",
    "ChunkKind",
    "ContentBlock",
    # Context
    "ContextLoader",
    "HookContext",
    "HookPoint",
    "Message",
    # Config
    "ObscuraConfig",
    # Handlers
    "RequestHandler",
    "Role",
    "SessionRef",
    # Sessions
    "SessionStore",
    "SimpleHandler",
    "StreamChunk",
    "TokenRefresher",
    # Tools
    "ToolRegistry",
    "ToolSpec",
    "resolve_auth",
    "tool",
    # Kairos — autonomous goal runtime
    "Checkpoint",
    "CheckpointKind",
    "Goal",
    "GoalBudget",
    "GoalStatus",
    "Intervention",
    "InterventionKind",
    "Kairos",
    "KairosConfig",
    "KairosEvent",
    "KairosEventKind",
    "Plan",
    "PlanStatus",
    "Task",
    "TaskResult",
    "TaskStatus",
]
