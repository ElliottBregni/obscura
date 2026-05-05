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

    # Core wiring (built by build_core_session).
    # ``client`` is the legacy ObscuraClient handle. On the composition
    # path (post-Stage-4b), it's None — composition builds the backend
    # directly and stores it on _owned_backend/_owned_tool_registry. On
    # the Agent.start legacy path, ObscuraClient is still constructed
    # and held here for back-compat.
    client: Any = None  # ObscuraClient | None at type level

    # Surface-supplied callbacks (ask_user / permission_mode / etc.)
    # These are also threaded into client.host_callbacks at build time;
    # exposed here for blocks that need to reference them.
    host_callbacks: dict[str, Any] = field(default_factory=_empty_str_any_dict)

    # Live system prompt (mutated by install_repl_prompt_sections post-build).
    # Both Copilot and Claude backends read self._system_prompt per-stream,
    # so update_system_prompt propagates on the next turn.
    system_prompt: str = ""

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

    # ── Reliability state (set by composition/core.py during build) ──
    # These mirror what ObscuraClient holds today. After Stage 4b,
    # composition is the canonical owner — ObscuraClient (when present)
    # mirrors them.
    _capability_token: Any = None
    _circuit_registry: Any = None
    _cache: Any = None
    _current_loop: Any = None
    _max_retries: int = 2
    _retry_initial_backoff: float = 0.5

    # ── Direct backend ownership (Stage 4b) ──
    # When composition/core builds the backend directly (no ObscuraClient
    # in the path), these are populated and the accessors below prefer
    # them over reading from client._backend / client._tool_registry.
    _owned_backend: Any = None
    _owned_tool_registry: Any = None
    _owned_hooks: Any = None
    _owned_user: Any = None
    _owned_mcp_backend: Any = None
    _owned_system_prompt: str = ""

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
        """The live tool registry.

        Composition path: returns ``_owned_tool_registry``.
        Legacy ObscuraClient path: returns ``client._tool_registry``.
        """
        if self._owned_tool_registry is not None:
            return self._owned_tool_registry
        return self.client._tool_registry

    @property
    def backend(self) -> Any:
        """The active LLM backend.

        Composition path: returns ``_owned_backend`` (set by
        ``build_core_session``). Legacy path: returns ``client._backend``.
        """
        if self._owned_backend is not None:
            return self._owned_backend
        return self.client._backend

    @property
    def hooks(self) -> Any:
        """The project hook registry threaded into the agent loop."""
        if self._owned_backend is not None:
            return self._owned_hooks
        return getattr(self.client, "_hooks", None)

    @hooks.setter
    def hooks(self, value: Any) -> None:
        if self._owned_backend is not None:
            self._owned_hooks = value
        else:
            self.client._hooks = value

    @property
    def user(self) -> Any:
        """The authenticated user (or None for unauth surfaces)."""
        if self._owned_backend is not None:
            return self._owned_user
        return getattr(self.client, "_user", None)

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
        self.backend.register_tool(spec)
        return True

    def register_tool(self, spec: ToolSpec) -> bool:
        """Public alias for ``add_tool``. Forwards to the same de-duped
        registration so ObscuraClient-style ``client.register_tool(spec)``
        callers can migrate to ``session.register_tool(spec)``.
        """
        return self.add_tool(spec)

    def list_tools(self) -> list[ToolSpec]:
        """Return the active tool specs."""
        return self.registry.all()

    def update_system_prompt(self, prompt: str) -> None:
        """Mutate the active system prompt post-build.

        Both Copilot and Claude backends read ``self._system_prompt`` at
        each stream call (not at start), so mutation here propagates on
        the next turn.
        """
        self.system_prompt = prompt
        self._owned_system_prompt = prompt
        # Mirror to client when present (legacy path)
        if self.client is not None:
            self.client._system_prompt = prompt
        # Mirror to backend (works for both composition and legacy paths)
        backend = self.backend
        if hasattr(backend, "_system_prompt"):
            setattr(backend, "_system_prompt", prompt)  # noqa: B010

    def update_owned_system_prompt(self, prompt: str) -> None:
        """Set ``_owned_system_prompt`` (composition path).

        Composition surfaces use this rather than touching the protected
        attribute directly. Equivalent to ``update_system_prompt`` but
        explicit about not mutating the legacy client.
        """
        self.system_prompt = prompt
        self._owned_system_prompt = prompt
        backend = self.backend
        if hasattr(backend, "_system_prompt"):
            setattr(backend, "_system_prompt", prompt)  # noqa: B010

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
        """Idempotent. Tear down resources LIFO, then close backend/client."""
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            for closer in reversed(self._resources):
                try:
                    await closer.aclose()
                except Exception:
                    logger.exception("teardown failed for %r", closer)
            # Composition path: stop backend directly (and any owned MCP)
            if self._owned_backend is not None:
                if self._owned_mcp_backend is not None:
                    try:
                        await self._owned_mcp_backend.stop()
                    except Exception:
                        logger.exception("mcp backend stop failed")
                try:
                    await self._owned_backend.stop()
                except Exception:
                    logger.exception("backend stop failed")
                return
            # Legacy path: close ObscuraClient
            if self.client is not None:
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
        """Stream agent events for a prompt.

        Constructs ``make_agent_loop`` directly using session state
        (capability_token, hooks, host_callbacks). Mirror of the legacy
        ``ObscuraClient.run_loop`` body. For Claude SDK backends,
        on_confirm is rerouted to the backend's PreToolUse hook (Claude
        executes tools internally via MCP, so the loop's confirmation
        gate is never reached).
        """
        from obscura.core.agent_loop_factory import make_agent_loop
        from obscura.core.context import load_session_messages
        from obscura.core.paths import resolve_obscura_home
        from obscura.core.types import ConfirmationCapable, ToolCallInfo

        sid = session_id or self.session_id

        load_history = bool(kwargs.pop("load_session_history", True))
        initial_messages = kwargs.pop("initial_messages", None)
        if load_history and initial_messages is None and sid:
            try:
                db_path = resolve_obscura_home() / "events.db"
                initial_messages = load_session_messages(
                    sid, db_path, max_turns=5,
                )
            except Exception:
                logger.debug("stream_loop: history load failed", exc_info=True)
                initial_messages = None

        backend = self.backend
        loop_confirm = on_confirm
        if on_confirm is not None and isinstance(backend, ConfirmationCapable):
            def _wrap_confirm(name: str, inp: dict[str, Any]) -> bool:
                result = on_confirm(
                    ToolCallInfo(tool_use_id="", name=name, input=inp),
                )
                return bool(result)

            backend.enable_confirmation(_wrap_confirm)
            loop_confirm = None
            # Cast back to BackendProtocol for make_agent_loop typing
            from typing import cast as _cast

            from obscura.core.types import BackendProtocol

            backend = _cast(BackendProtocol, backend)

        ctx_window = getattr(backend, "context_window", 0) or 0
        context_budget = kwargs.pop("context_budget", 0)
        if not context_budget and ctx_window:
            context_budget = int(ctx_window * 0.50 * 4)

        backend_type = getattr(backend, "backend_type", None)
        backend_name = getattr(backend_type, "value", "") or self.config.backend
        model = getattr(backend, "model", None) or self.config.model or ""

        event_store = kwargs.pop("event_store", None)
        auto_complete = kwargs.pop("auto_complete", True)
        tool_allowlist = kwargs.pop("tool_allowlist", None)

        loop = make_agent_loop(
            backend,
            self.registry,
            max_turns=max_turns or self.config.max_turns,
            on_confirm=loop_confirm,
            capability_token=self._capability_token,
            hooks=self.hooks,
            event_store=event_store,
            auto_complete=auto_complete,
            backend_name=backend_name,
            model_name=model,
            context_budget=context_budget,
            tool_allowlist=tool_allowlist,
            host_callbacks=self.host_callbacks,
        )
        self._current_loop = loop
        return loop.run(
            prompt,
            session_id=sid or "",
            initial_messages=initial_messages,
            **kwargs,
        )

    # ── Client surface — direct implementation (Stage 3) ─────────────
    # send / stream now own their bodies (cache + retry + circuit
    # breaker + telemetry) using session state instead of forwarding
    # to client. The session is the authority; ObscuraClient becomes a
    # back-compat shim around the session.

    async def send(self, prompt: str, **kwargs: Any) -> Any:
        """Non-streaming single-turn request.

        Cache → circuit breaker → retry → backend.send. Mirror of the
        legacy ``ObscuraClient.send`` body. When ``self._cache`` is set
        (opt-in), a hit short-circuits the backend call. The
        ``_circuit_registry`` per-backend gate prevents thundering-herd
        when a provider is degraded.
        """
        from obscura.core.enums.agent import Role
        from obscura.core.retry import with_retry
        from obscura.core.types import ContentBlock, Message

        backend = self.backend
        backend_type = getattr(backend, "backend_type", None)
        backend_name = getattr(backend_type, "value", "") or self.config.backend
        model = getattr(backend, "model", None) or self.config.model or ""
        sys_prompt = self.system_prompt or self.config.system_prompt

        # Cache (opt-in, set via configure_cache or composition core)
        cache = self._cache
        cache_key = ""
        if cache is not None:
            from obscura.core.llm_cache import LLMCache

            cache_key = LLMCache.make_key(
                backend_name, model, sys_prompt, prompt,
            )
            cached = cache.get(cache_key)
            if cached is not None:
                return Message(
                    role=Role.ASSISTANT,
                    content=[ContentBlock(kind="text", text=cached.response_text)],
                )

        circuit = (
            self._circuit_registry.get(backend_name)
            if self._circuit_registry is not None
            else None
        )

        result = await with_retry(
            backend.send,
            prompt,
            max_retries=self._max_retries,
            initial_backoff=self._retry_initial_backoff,
            circuit=circuit,
            **kwargs,
        )

        if cache is not None and cache_key:
            text = getattr(result, "text", "")
            if text:
                cache.put(cache_key, text, backend=backend_name, model=model)

        return result

    async def stream(self, prompt: str, **kwargs: Any) -> AsyncIterator[Any]:
        """Streaming single-turn request.

        Circuit breaker gate (no retry on streams; the chunks have
        already started). Mirror of legacy ``ObscuraClient.stream``.
        """
        from obscura.core.circuit_breaker import CircuitOpenError

        backend = self.backend
        backend_type = getattr(backend, "backend_type", None)
        backend_name = getattr(backend_type, "value", "") or self.config.backend

        if self._circuit_registry is not None:
            circuit = self._circuit_registry.get(backend_name)
            if not circuit.allow_request():
                raise CircuitOpenError(
                    circuit.name, circuit.time_until_half_open(),
                )
            try:
                async for chunk in backend.stream(prompt, **kwargs):
                    yield chunk
                circuit.record_success()
            except Exception:
                circuit.record_failure()
                raise
        else:
            async for chunk in backend.stream(prompt, **kwargs):
                yield chunk

    async def resume_session(self, ref: Any) -> None:
        """Resume a prior backend session. Forwards to client.resume_session."""
        await self.client.resume_session(ref)

    async def delete_session(self, ref: Any) -> None:
        """Delete a backend session. Forwards to client.delete_session."""
        await self.client.delete_session(ref)

    async def create_backend_session(self) -> Any:
        """Create a fresh backend session ref. Forwards to client.create_session.

        Renamed from ``create_session`` to avoid colliding with the
        composition-layer concept of "session" (which is *this* object,
        ``AgentSession``).
        """
        return await self.client.create_session()

    @property
    def capability_tier(self) -> str:
        """Resolved capability tier (from the underlying client)."""
        return self.client.capability_tier or ""

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
