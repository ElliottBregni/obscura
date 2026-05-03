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

import asyncio
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
    """Reference to the live conversation history list (mutable).

    Tools that mutate this list should serialize through :meth:`append_history`
    (or otherwise acquire :attr:`history_lock`) so that two parallel tool
    calls executing under a future DAG executor don't race on the underlying
    list.
    """

    history_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    """Cross-task lock for serializing :attr:`history` mutations.

    Uncontended in the common case (sequential tool execution today). Held
    only across the actual list mutation, never across ``await`` boundaries
    that could deadlock. Required once the agent loop runs tools in parallel
    under a DAG executor — without it, two tools that both append to
    ``history`` (e.g. via ``history_snip`` or follow-on patterns) would race
    on Python's list internals.
    """

    user: Any = None
    """The authenticated user (AuthenticatedUser or None)."""

    session_id: str | None = None
    """Durable session identifier, if one is active."""

    # Host-supplied callbacks. Tools that need user interaction or permission-mode
    # changes read these from ToolContext when present and fall back to legacy
    # module-level globals (set by ``set_*_callback`` helpers) when they're None.
    ask_user_callback: Any = None
    """Callback for ``ask_user`` / ``user_ask`` tools. Signature varies by host."""

    user_interact_callback: Any = None
    """Callback for the ``user_interact`` tool — generic UI prompts."""

    permission_mode_callback: Any = None
    """Callback for ``enter_plan_mode`` / ``exit_plan_mode``. Signature: (mode: str) -> None."""

    plan_approval_callback: Any = None
    """Callback gating exit from plan mode. Signature: (summary: str) -> bool|coroutine."""

    mcp_discovery_report: Any = None
    """Snapshot of the active backend's MCP discovery report (or None).

    Read by the ``mcp_discovery_status`` system tool so an agent can
    surface which external MCP servers came up, which failed, and why.
    """

    extras: dict[str, Any] = field(default_factory=lambda: {})
    """Backend-specific or future extension fields."""

    async def append_history(self, message: Any) -> None:
        """Append *message* to :attr:`history` under :attr:`history_lock`.

        Cross-task safe: two parallel tools that both call this won't race
        on the underlying list. Lock is uncontended in the common case
        (sequential tool execution); contention only arises once the agent
        loop runs tools in parallel under a DAG executor.

        No-op when :attr:`history` is ``None`` (no message list bound).
        """
        if self.history is None:
            return
        async with self.history_lock:
            self.history.append(message)


_current: ContextVar[ToolContext | None] = ContextVar(
    "obscura_tool_context", default=None
)


def current_tool_context() -> ToolContext | None:
    """Return the ToolContext bound to the current async task, or None.

    Returns None when no context is bound — tools should treat this as a
    soft failure and fall back to legacy module-level state if available.
    """
    return _current.get()


@contextlib.contextmanager  # pyright: ignore[reportDeprecated]
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
