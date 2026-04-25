"""obscura.core.tool_context — Per-call tool context via ContextVar.

Tools that need session-level state (registry, conversation history,
authenticated user) read it from a context var bound by the agent loop
around each tool invocation. This replaces the older pattern of
module-level globals + setters that had to be wired up by the REPL —
that pattern silently broke when the wiring was forgotten.

Usage in a tool::

    from obscura.core.tool_context import current_tool_context

    @tool("my_tool", "...")
    async def my_tool(arg: str) -> str:
        ctx = current_tool_context()
        if ctx is None or ctx.registry is None:
            return _json_error("no_context", ...)
        ...

The agent loop binds the context::

    with bind_tool_context(ToolContext(registry=..., history=...)):
        result = await spec.handler(**args)

ContextVar isolates state per asyncio task, so concurrent tool calls
across multiple agents in the same process don't fight over a shared
global.
"""

from __future__ import annotations

import contextlib
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator

    from obscura.core.tools import ToolRegistry


@dataclass(frozen=True)
class ToolContext:
    """Per-call session state available to tool handlers.

    Frozen by design — bind a new ToolContext per call instead of mutating
    fields. The ``history`` field holds a *reference* to a mutable list, so
    tools that need to edit conversation history (e.g. history_snip) can
    do so without violating frozenness of the ToolContext itself.
    """

    registry: ToolRegistry | None = None
    """The active ToolRegistry. Used by tool_search and similar discovery tools."""

    history: list[Any] | None = None
    """Reference to the live conversation history list (mutable)."""

    user: Any = None
    """The authenticated user (AuthenticatedUser or None)."""

    session_id: str | None = None
    """Durable session identifier, if one is active."""

    extras: dict[str, Any] = field(default_factory=lambda: {})
    """Backend-specific or future extension fields."""


_current: ContextVar[ToolContext | None] = ContextVar(
    "obscura_tool_context", default=None
)


def current_tool_context() -> ToolContext | None:
    """Return the ToolContext bound to the current async task, or None.

    Returns None when no context is bound — tools should treat this as a
    soft failure and fall back to legacy module-level state if available.
    """
    return _current.get()


@contextlib.contextmanager
def bind_tool_context(ctx: ToolContext) -> Iterator[None]:
    """Bind *ctx* for the duration of the with block.

    Uses ContextVar so the binding is isolated to the current async task —
    concurrent tool calls in other tasks will not see this context.
    """
    token = _current.set(ctx)
    try:
        yield
    finally:
        _current.reset(token)
