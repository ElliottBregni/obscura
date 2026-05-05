"""obscura.composition.session — AgentSession + SessionConfig.

`AgentSession` is the surface-agnostic agent handle every entry point
(REPL, API, A2A, MCP-server) builds via `build_*_session()` functions.

It owns:
- the constructed `ObscuraClient` (backend + tool registry + MCP wiring)
- a frozen `SessionConfig` snapshot
- a LIFO resource-teardown queue (`register_resource(...)`)
- thin `run_loop` / `stream_loop` wrappers so callers don't need to
  reach into the client

Building blocks (see `composition/blocks/`) take `(session, config)` and
mutate the session — registering tools, setting fields like
`vector_store`, registering resources for teardown.

Lifecycle:
    async with await build_repl_session(config) as session:
        async for event in session.stream_loop(prompt):
            ...
    # session.aclose() runs automatically: each resource's __aexit__/
    # close()/cancel is invoked LIFO, then the underlying client.

The session is intentionally idempotent under repeat-block-call:
`session.add_tool(spec)` is a no-op if a tool of that name is already
registered, so blocks can run twice (e.g. plugin reload) without
double-binding.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol, override

if TYPE_CHECKING:
    from obscura.core.client import ObscuraClient
    from obscura.core.tools import ToolRegistry
    from obscura.core.types import AgentEvent, ToolSpec

logger = logging.getLogger(__name__)

Surface = Literal["repl", "api", "a2a", "mcp_server"]


def _empty_mcp_servers() -> list[dict[str, Any]]:
    return []


def _empty_str_any_dict() -> dict[str, Any]:
    return {}


@dataclass
class SessionConfig:
    """Frozen-by-convention input to `build_*_session()`.

    Surface-specific extras (e.g. REPL `compiled_ws`, FastAPI request)
    go in `extras` to keep the dataclass shape stable across surfaces.
    Building blocks read from `extras` for surface-specific knobs.
    """

    backend: str = ""
    model: str | None = None
    system_prompt: str = ""
    tools_enabled: bool = True
    confirm_enabled: bool = True
    max_turns: int = 10
    inject_claude_context: bool = True

    # MCP servers explicitly passed (REPL discovers via _discover_mcp;
    # API/A2A get them from request body or config respectively)
    mcp_servers: list[dict[str, Any]] = field(default_factory=_empty_mcp_servers)

    # Surface-specific knobs (compiled_ws, request, oauth_token, etc.)
    extras: dict[str, Any] = field(default_factory=_empty_str_any_dict)


class _Closer(Protocol):
    """Internal protocol normalising the four supported teardown shapes."""

    async def aclose(self) -> None: ...


class _CallableCloser:
    """Wraps a no-arg `async def teardown() -> None`."""

    def __init__(self, fn: Callable[[], Awaitable[None]], name: str) -> None:
        self._fn = fn
        self._name = name

    async def aclose(self) -> None:
        await self._fn()

    @override
    def __repr__(self) -> str:
        return f"<callable-closer {self._name!r}>"


class _TaskCloser:
    """Wraps an asyncio.Task — cancel + await + suppress CancelledError."""

    def __init__(self, task: asyncio.Task[Any], name: str) -> None:
        self._task = task
        self._name = name

    async def aclose(self) -> None:
        if self._task.done():
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self._task

    @override
    def __repr__(self) -> str:
        return f"<task-closer {self._name!r}>"


class _ContextCloser:
    """Wraps an async-CM that's already been __aenter__'d, or a closeable."""

    def __init__(self, cm: Any, name: str) -> None:
        self._cm = cm
        self._name = name

    async def aclose(self) -> None:
        cm = self._cm
        if hasattr(cm, "__aexit__"):
            await cm.__aexit__(None, None, None)
        elif hasattr(cm, "aclose"):
            await cm.aclose()
        elif hasattr(cm, "close"):
            res = cm.close()
            if asyncio.iscoroutine(res):
                await res

    @override
    def __repr__(self) -> str:
        return f"<ctx-closer {self._name!r}>"


def _empty_resources() -> list[_Closer]:
    return []


@dataclass
class AgentSession:
    """Per-surface agent handle. Built by `build_*_session()` functions.

    Surfaces interact with the session, NOT with `ObscuraClient` directly,
    so future migrations can chip away at the client without breaking
    surface code.
    """

    # Identity
    session_id: str
    surface: Surface

    # Frozen config
    config: SessionConfig

    # Core wiring (built by build_core_session)
    client: ObscuraClient

    # Surface-supplied callbacks (ask_user / permission_mode / etc.)
    # These are also threaded into client.host_callbacks at build time;
    # exposed here for blocks that need to reference them.
    host_callbacks: dict[str, Any] = field(default_factory=_empty_str_any_dict)

    # Optional features (None when the relevant block opted out)
    vector_store: Any = None
    context_router: Any = None
    turn_classifier: Any = None
    capability_resolver: Any = None
    project_hooks: Any = None
    tool_router: Any = None
    browser_bridge: Any = None
    supervisor: Any = None
    supervisor_task: Any = None  # asyncio.Task[None] | None
    kairos_engine: Any = None
    uds_inbox: Any = None
    imessage_daemon_task: Any = None  # asyncio.Task[None] | None

    # Resource teardown queue (LIFO)
    _resources: list[_Closer] = field(default_factory=_empty_resources)
    _closed: bool = False
    _close_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    # ── Tool registration ─────────────────────────────────────────────
    # NOTE: AgentSession reaches into ObscuraClient's _tool_registry and
    # _backend below. This is the deliberate intermediate state of the
    # composition refactor: ObscuraClient still owns the registry/backend
    # today, AgentSession is the new surface-facing handle. As blocks
    # migrate, more state moves onto AgentSession and these accessors
    # collapse. Suppressing `reportPrivateUsage` here is intentional and
    # tracked.

    @property
    def registry(self) -> ToolRegistry:
        """The live tool registry (owned by the client)."""
        return self.client._tool_registry  # pyright: ignore[reportPrivateUsage]

    def add_tool(self, spec: ToolSpec) -> bool:
        """Register a tool with the underlying client + backend.

        Returns True if newly added, False if a tool of that name was
        already present (idempotent — blocks may safely run twice).
        """
        existing = {t.name for t in self.registry.all()}
        if spec.name in existing:
            return False
        self.registry.register(spec)
        # Mirror to backend so it shows up in tool-use prompts
        self.client._backend.register_tool(spec)  # pyright: ignore[reportPrivateUsage]
        return True

    # ── Lifecycle ────────────────────────────────────────────────────

    def register_resource(
        self,
        closer: Any,
        *,
        name: str = "",
    ) -> None:
        """Register a resource for LIFO teardown by `aclose()`.

        Accepts: an async context manager (already entered), an
        `asyncio.Task`, an object with `.aclose()` or `.close()`, or
        a no-arg `async def teardown() -> None` callable.
        """
        label = name or repr(closer)
        wrapped: _Closer
        if isinstance(closer, asyncio.Task):
            task: asyncio.Task[Any] = closer  # pyright: ignore[reportUnknownVariableType]
            wrapped = _TaskCloser(task, label)
        elif callable(closer) and not hasattr(closer, "__aexit__"):
            # Plain async callable (after __aexit__ check so async-CMs go
            # to ContextCloser, since they're also "callable" for some defs)
            wrapped = _CallableCloser(
                closer,  # pyright: ignore[reportArgumentType]
                label,
            )
        else:
            wrapped = _ContextCloser(closer, label)
        self._resources.append(wrapped)

    async def __aenter__(self) -> AgentSession:
        return self

    async def __aexit__(
        self,
        _exc_type: Any,
        _exc: Any,
        _tb: Any,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Idempotent. Tear down resources LIFO, then close the client."""
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            for closer in reversed(self._resources):
                try:
                    await closer.aclose()
                except Exception:
                    logger.exception("teardown failed for %r", closer)
            try:
                await self.client.__aexit__(None, None, None)
            except Exception:
                logger.exception("client close failed")

    # ── Agent loop wrappers ───────────────────────────────────────────

    def stream_loop(
        self,
        prompt: str,
        *,
        max_turns: int | None = None,
        on_confirm: Callable[..., Any] | None = None,
        session_id: str | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[AgentEvent]:
        """Stream agent events for a prompt. Forwards to `client.run_loop`."""
        return self.client.run_loop(
            prompt,
            max_turns=max_turns or self.config.max_turns,
            on_confirm=on_confirm,
            session_id=session_id or self.session_id,
            **kwargs,
        )

    async def run_loop_to_text(
        self,
        prompt: str,
        *,
        max_turns: int | None = None,
        on_confirm: Callable[..., Any] | None = None,
        session_id: str | None = None,
        **kwargs: Any,
    ) -> str:
        """Run to completion and return the final assistant text.

        Concatenates `TEXT_DELTA` events for the final turn. Useful for
        non-streaming surfaces (A2A blocking `message/send`, MCP RPC).
        """
        from obscura.core.enums.agent import AgentEventKind

        chunks: list[str] = []
        last_turn_text: list[str] = []
        async for event in self.stream_loop(
            prompt,
            max_turns=max_turns,
            on_confirm=on_confirm,
            session_id=session_id,
            **kwargs,
        ):
            if event.kind == AgentEventKind.TURN_START:
                last_turn_text = []
            elif event.kind == AgentEventKind.TEXT_DELTA:
                last_turn_text.append(event.text or "")
            elif event.kind == AgentEventKind.TURN_COMPLETE:
                chunks = last_turn_text  # keep only final turn's text
        return "".join(chunks)


def new_session_id() -> str:
    """Generate a stable session id (uuid4 hex)."""
    return uuid.uuid4().hex
