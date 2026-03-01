"""
obscura.client — ObscuraClient: unified entry point for all backends.

Dispatches to the appropriate backend (Copilot, Claude, OpenAI, Moonshot, LocalLLM)
based on the ``backend`` parameter. Integrates with ``copilot_models`` for
model alias resolution and safety guards.
"""

from __future__ import annotations

import logging
from typing import Any, AsyncIterator, Awaitable, Callable, cast

_logger = logging.getLogger(__name__)

from obscura.core.auth import AuthConfig, resolve_auth
from obscura.core.tools import ToolRegistry
from obscura.core.types import (
    AgentEvent,
    Backend,
    BackendCapabilities,
    BackendProtocol,
    HookPoint,
    Message,
    NativeHandle,
    SessionRef,
    StreamChunk,
    ToolSpec,
)
from obscura.core.tool_policy import ToolPolicy


# ---------------------------------------------------------------------------
# Unified client
# ---------------------------------------------------------------------------


class ObscuraClient:
    """Unified SDK client that dispatches to any backend.

    Usage::

        async with ObscuraClient("copilot", model_alias="copilot_automation_safe") as client:
            response = await client.send("explain this code")

        async with ObscuraClient("claude", model="claude-sonnet-4-5-20250929") as client:
            async for chunk in client.stream("count to 5"):
                print(chunk.text, end="", flush=True)

        async with ObscuraClient("openai", model="gpt-4o") as client:
            response = await client.send("summarize this")

        async with ObscuraClient("codex", model="gpt-5") as client:
            response = await client.send("summarize this")

        async with ObscuraClient("moonshot", model="kimi-2.5") as client:
            response = await client.send("summarize this")

        async with ObscuraClient("localllm") as client:
            response = await client.send("hello from localhost")
    """

    def __init__(
        self,
        backend: Backend | str,
        *,
        auth: AuthConfig | None = None,
        model: str | None = None,
        model_alias: str | None = None,
        automation_safe: bool = False,
        system_prompt: str = "",
        tools: list[ToolSpec] | None = None,
        mcp_servers: list[dict[str, Any]] | None = None,
        # Skill loading
        lazy_load_skills: bool = False,
        skill_filter: list[str] | None = None,
        # Claude-specific
        permission_mode: str = "default",
        cwd: str | None = None,
        # Copilot-specific
        streaming: bool = True,
        # Tool policy
        tool_policy: ToolPolicy | None = None,
        # HTTP server context
        user: object | None = None,
        # Prompt injection control
        inject_tier_prompt: bool = False,  # opt-in: prepend tier system prompt
        inject_claude_context: bool = False,  # opt-in: load ~/.claude/ context
    ) -> None:
        if isinstance(backend, str):
            backend = Backend(backend)
        self._backend_type = backend
        self._user = user
        self._tool_policy = tool_policy or ToolPolicy.custom_only()  # Default: block native tools

        # Resolve model via copilot_models aliases
        resolved_model = self._resolve_model(
            backend,
            model,
            model_alias,
            automation_safe,
        )

        # Resolve auth (pass user for per-identity scoping)
        resolved_auth = resolve_auth(backend, auth, user=user)

        # Build tool registry
        self._tool_registry = ToolRegistry()
        for t in tools or []:
            self._tool_registry.register(t)

        # Resolve capability tier and generate token (identity-based, not prompt-based)
        self._capability_token = None
        if user is not None:
            try:
                from obscura.auth.capability import generate_capability_token
                from obscura.auth.models import AuthenticatedUser as _AuthUser
                from obscura.auth.system_prompts import get_tier_system_prompt
                import uuid as _uuid

                if isinstance(user, _AuthUser):
                    session_id = _uuid.uuid4().hex
                    self._capability_token = generate_capability_token(user, session_id)
                    if inject_tier_prompt:
                        # Inject tier-appropriate system prompt
                        system_prompt = get_tier_system_prompt(
                            self._capability_token.tier,
                            additional=system_prompt,
                        )
            except Exception:
                pass  # Degrade gracefully if capability module not available

        # Inject ~/.claude context (CLAUDE.md + instructions + skills)
        # Off by default — Obscura uses its own system prompts
        if inject_claude_context:
            try:
                from obscura.core.context import ContextLoader
                loader = ContextLoader(
                    backend,
                    lazy_load_skills=lazy_load_skills,
                    skill_filter=skill_filter,
                )
                claude_ctx = loader.load_system_prompt()
                if claude_ctx:
                    system_prompt = f"{claude_ctx}\n\n{system_prompt}" if system_prompt else claude_ctx
            except Exception:
                pass

        # -- Reliability infrastructure ------------------------------------------
        # Circuit breaker (per-backend, shared via registry if passed in)
        from obscura.core.circuit_breaker import CircuitBreakerRegistry

        self._circuit_registry = CircuitBreakerRegistry()

        # Retry config
        self._max_retries = 2
        self._retry_initial_backoff = 0.5

        # LLM cache (opt-in, set via configure_cache)
        self._cache: Any = None  # LLMCache | None
        self._model = resolved_model or ""
        self._system_prompt = system_prompt

        # Current agent loop (set during run_loop, exposed for mid-run input)
        self._current_loop: Any = None

        # Store MCP server configs for lazy initialization in start().
        # MCP tools are connected via Obscura's own MCPBackend so they
        # appear in the ToolRegistry and work with AgentLoop.
        self._mcp_server_configs = mcp_servers or []
        self._mcp_backend: Any = None  # MCPBackend, set in start()

        # Create backend (mcp_servers NOT forwarded — Obscura handles them)
        self._backend = self._create_backend(
            backend=backend,
            auth=resolved_auth,
            model=resolved_model,
            system_prompt=system_prompt,
            mcp_servers=None,
            permission_mode=permission_mode,
            cwd=cwd,
            streaming=streaming,
            tool_policy=self._tool_policy,
        )

        # Register tools with backend (filtered by capability tier)
        tier_value = (
            self._capability_token.tier.value
            if self._capability_token is not None
            else "public"
        )
        for t in self._tool_registry.for_tier(tier_value):
            self._backend.register_tool(t)

    # -- Lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        """Initialize the backend connection and MCP servers.

        MCP servers are connected BEFORE the backend starts so that all
        tools (system + MCP) are registered and visible to the backend
        when it builds its options (e.g. Claude SDK tool listing and
        system prompt).
        """
        # Connect to MCP servers and register their tools FIRST
        if self._mcp_server_configs:
            from obscura.integrations.mcp.types import (
                MCPConnectionConfig,
                MCPTransportType,
            )
            from obscura.providers.mcp_backend import MCPBackend

            configs: list[MCPConnectionConfig] = []
            for server in self._mcp_server_configs:
                transport = MCPTransportType(server.get("transport", "stdio"))
                configs.append(
                    MCPConnectionConfig(
                        transport=transport,
                        command=server.get("command"),
                        args=server.get("args", []),
                        url=server.get("url"),
                        env=server.get("env", {}),
                        name=server.get("name", ""),
                    )
                )

            self._mcp_backend = MCPBackend(configs)
            await self._mcp_backend.start()

            mcp_tools = self._mcp_backend.list_tools()
            for spec in mcp_tools:
                self._tool_registry.register(spec)
                self._backend.register_tool(spec)

            if not mcp_tools:
                _logger.warning(
                    "MCP servers were configured but no tools were registered. "
                    "Check server connectivity. Connection errors: %s",
                    self._mcp_backend.connection_errors,
                )

        # NOW start the backend — all tools are registered, so the Claude
        # SDK will build its MCP server and system prompt with full tool info.
        await self._backend.start()

    async def stop(self) -> None:
        """Gracefully shut down."""
        if self._mcp_backend is not None:
            await self._mcp_backend.stop()
            self._mcp_backend = None
        await self._backend.stop()

    async def __aenter__(self) -> ObscuraClient:
        await self.start()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.stop()

    # -- Query ---------------------------------------------------------------

    async def send(self, prompt: str, **kwargs: Any) -> Message:
        """Send prompt, wait for full response."""
        import time as _time

        from obscura.core.retry import with_retry

        # Apply prompt injection filter and memory enrichment
        prompt = self._enrich_prompt(self._filter_prompt(prompt))

        # Check cache (opt-in)
        if self._cache is not None:
            from obscura.core.llm_cache import LLMCache

            cache: LLMCache = self._cache
            cache_key = LLMCache.make_key(
                self._backend_type.value, self._model, self._system_prompt, prompt
            )
            cached = cache.get(cache_key)
            if cached is not None:
                _record_cache_hit(self._backend_type.value)
                from obscura.core.types import ContentBlock, Role

                return Message(
                    role=Role.ASSISTANT,
                    content=[ContentBlock(kind="text", text=cached.response_text)],
                )
        else:
            cache_key = ""

        circuit = self._circuit_registry.get(self._backend_type.value)

        tracer = _get_client_tracer()
        with tracer.start_as_current_span("obscura.core.client.send") as span:
            _set_span_attr(span, "obscura.backend", self._backend_type.value)
            _set_span_attr(span, "obscura.method", "send")
            start = _time.monotonic()
            try:
                result = await with_retry(
                    self._backend.send,
                    prompt,
                    max_retries=self._max_retries,
                    initial_backoff=self._retry_initial_backoff,
                    circuit=circuit,
                    **kwargs,
                )
                duration = _time.monotonic() - start
                _record_request_metric(self._backend_type.value, "send", "success")
                _record_request_duration(self._backend_type.value, "send", duration)

                # Store in cache
                if self._cache is not None and cache_key:
                    text = getattr(result, "text", "")
                    if text:
                        self._cache.put(
                            cache_key,
                            text,
                            backend=self._backend_type.value,
                            model=self._model,
                        )

                return result
            except Exception:
                duration = _time.monotonic() - start
                _record_request_metric(self._backend_type.value, "send", "error")
                _record_request_duration(self._backend_type.value, "send", duration)
                raise

    async def stream(self, prompt: str, **kwargs: Any) -> AsyncIterator[StreamChunk]:
        """Send prompt, yield streaming chunks."""
        import time as _time

        from obscura.core.circuit_breaker import CircuitOpenError

        # Apply prompt injection filter and memory enrichment
        prompt = self._enrich_prompt(self._filter_prompt(prompt))

        # Check circuit breaker before streaming (no retry on streams)
        circuit = self._circuit_registry.get(self._backend_type.value)
        if not circuit.allow_request():
            raise CircuitOpenError(
                circuit.name, circuit.time_until_half_open()
            )

        tracer = _get_client_tracer()
        span = tracer.start_span("obscura.core.client.stream")
        _set_span_attr(span, "obscura.backend", self._backend_type.value)
        _set_span_attr(span, "obscura.method", "stream")
        start = _time.monotonic()
        status = "success"
        try:
            async for chunk in self._backend.stream(prompt, **kwargs):
                _record_stream_chunk(self._backend_type.value, chunk.kind.value)
                yield chunk
            circuit.record_success()
        except Exception:
            status = "error"
            circuit.record_failure()
            raise
        finally:
            duration = _time.monotonic() - start
            _record_request_metric(self._backend_type.value, "stream", status)
            _record_request_duration(self._backend_type.value, "stream", duration)
            span.end()

    # -- Agent loop ----------------------------------------------------------

    def run_loop(
        self,
        prompt: str,
        *,
        max_turns: int = 10,
        on_confirm: Callable[..., Any] | None = None,
        event_store: Any | None = None,
        session_id: str | None = None,
        auto_complete: bool = True,
        load_session_history: bool = True,
        **kwargs: Any,
    ) -> AsyncIterator[AgentEvent]:
        """Run an iterative agent loop with automatic tool execution.

        The model streams its response, and when it calls a tool, the loop
        executes the tool handler, feeds the result back, and lets the model
        continue — up to *max_turns* iterations.

        Usage::

            async for event in client.run_loop("Fix the auth bug", max_turns=5):
                if event.kind == AgentEventKind.TEXT_DELTA:
                    print(event.text, end="")
                elif event.kind == AgentEventKind.TOOL_CALL:
                    print(f"Calling {event.tool_name}...")

        Parameters
        ----------
        prompt:
            The initial user prompt.
        max_turns:
            Maximum number of model turns (default 10).
        on_confirm:
            Optional callback ``(ToolCallInfo) -> bool`` invoked before
            each tool execution. Return False to deny.
        event_store:
            Optional :class:`EventStoreProtocol`.  When provided, events
            are persisted to durable storage.
        session_id:
            Optional session ID for event persistence.
        auto_complete:
            When False, the loop will not mark the session COMPLETED
            on finish — the caller manages the session lifecycle.
        """
        from obscura.core.agent_loop import AgentLoop

        # Load session history if enabled
        initial_messages = None
        if load_session_history and session_id:
            try:
                from obscura.core.context import load_session_messages
                from obscura.core.paths import resolve_obscura_home
                db_path = resolve_obscura_home() / "events.db"
                initial_messages = load_session_messages(session_id, db_path, max_turns=5)
                if initial_messages:
                    _logger.debug(f"Loaded {len(initial_messages)} messages from session {session_id}")
            except Exception as e:
                _logger.warning(f"Could not load session history: {e}")


        # For Claude: route confirmation through PreToolUse hook instead of
        # AgentLoop.on_confirm (Claude SDK executes tools internally via MCP,
        # so the loop's confirmation gate is never reached).
        loop_confirm = on_confirm
        if on_confirm and self._backend_type == Backend.CLAUDE:
            from obscura.core.types import ToolCallInfo

            def _wrap_confirm(name: str, inp: dict[str, Any]) -> bool:
                return on_confirm(ToolCallInfo(name=name, input=inp))  # type: ignore[arg-type]

            self._backend.enable_confirmation(_wrap_confirm)
            loop_confirm = None  # don't double-gate

        # Default context budget: 50% of context window, in chars (~4 chars/token)
        context_budget = kwargs.pop("context_budget", 0)
        if not context_budget:
            context_budget = int(self.context_window * 0.50 * 4)

        loop = AgentLoop(
            self._backend,
            self._tool_registry,
            max_turns=max_turns,
            on_confirm=loop_confirm,
            capability_token=self._capability_token,
            event_store=event_store,
            auto_complete=auto_complete,
            backend_name=self._backend_type.value,
            model_name=self._model,
            context_budget=context_budget,
        )
        self._current_loop = loop
        return loop.run(prompt, session_id=session_id, initial_messages=initial_messages, **kwargs)

    async def run_loop_to_completion(
        self,
        prompt: str,
        *,
        max_turns: int = 10,
        on_confirm: Callable[..., Any] | None = None,
        **kwargs: Any,
    ) -> str:
        """Run the agent loop and return the final concatenated text."""
        from obscura.core.agent_loop import AgentLoop

        # Load session history if enabled
        initial_messages = None
        if load_session_history and session_id:
            try:
                from obscura.core.context import load_session_messages
                from obscura.core.paths import resolve_obscura_home
                db_path = resolve_obscura_home() / "events.db"
                initial_messages = load_session_messages(session_id, db_path, max_turns=5)
                if initial_messages:
                    _logger.debug(f"Loaded {len(initial_messages)} messages from session {session_id}")
            except Exception as e:
                _logger.warning(f"Could not load session history: {e}")


        loop = AgentLoop(
            self._backend,
            self._tool_registry,
            max_turns=max_turns,
            on_confirm=on_confirm,
            capability_token=self._capability_token,
            backend_name=self._backend_type.value,
            model_name=self._model,
        )
        return await loop.run_to_completion(prompt, **kwargs)

    # -- Sessions ------------------------------------------------------------

    async def create_session(self, **kwargs: Any) -> SessionRef:
        """Create a new persistent session."""
        return await self._backend.create_session(**kwargs)

    async def resume_session(self, ref: SessionRef) -> None:
        """Resume a previously created session."""
        await self._backend.resume_session(ref)

    async def list_sessions(self) -> list[SessionRef]:
        """List available sessions."""
        return await self._backend.list_sessions()

    async def delete_session(self, ref: SessionRef) -> None:
        """Delete a session."""
        await self._backend.delete_session(ref)

    async def fork_session(self, ref: SessionRef) -> SessionRef:
        """Fork an existing session.

        Uses backend-native fork when implemented, otherwise performs a
        logical fork fallback by creating/resuming sessions where possible.
        """
        fork_fn = getattr(self._backend, "fork_session", None)
        if callable(fork_fn):
            typed_fork = cast(Callable[[SessionRef], Awaitable[SessionRef]], fork_fn)
            return await typed_fork(ref)
        raise RuntimeError(
            f"Backend {self._backend_type.value} does not support session forking."
        )

    # -- Tools ---------------------------------------------------------------

    def register_tool(self, spec: ToolSpec) -> None:
        """Register a tool with the active backend."""
        self._tool_registry.register(spec)
        self._backend.register_tool(spec)

    def list_tools(self) -> list[ToolSpec]:
        """Return all currently registered tool specs."""
        return self._tool_registry.all()

    # -- Hooks ---------------------------------------------------------------

    def on(self, hook: HookPoint, callback: Callable[..., Any]) -> None:
        """Register a hook callback."""
        self._backend.register_hook(hook, callback)

    # -- Backend access (escape hatch) --------------------------------------

    @property
    def backend_impl(self) -> BackendProtocol:
        """Direct access to the underlying backend for SDK-specific features.

        Example::

            claude = client.backend_impl  # ClaudeBackend
            await claude.fork_session(ref)
        """
        return self._backend

    @property
    def current_loop(self) -> Any | None:
        """The currently-running AgentLoop, if any.

        Exposed so the CLI can call ``inject_user_input()`` or
        ``request_pause()`` while the loop is streaming.
        """
        return self._current_loop

    @property
    def backend_type(self) -> Backend:
        """Which backend is active."""
        return self._backend_type

    @property
    def capability_tier(self) -> str | None:
        """Return the resolved capability tier, or None if not applicable."""
        if self._capability_token is not None:
            return self._capability_token.tier.value
        return None

    @property
    def native(self) -> NativeHandle:
        """Raw SDK access — passthrough to the active backend.

        Usage::

            handle = client.native
            raw_openai = handle.client  # AsyncOpenAI instance
        """
        return self._backend.native

    def capabilities(self) -> BackendCapabilities:
        """Declare what the active backend supports.

        Usage::

            caps = client.capabilities()
            if caps.supports_reasoning:
                ...
        """
        return self._backend.capabilities()

    # -- Reliability configuration -------------------------------------------

    def configure_retry(
        self, *, max_retries: int = 2, initial_backoff: float = 0.5
    ) -> None:
        """Set retry parameters for ``send()``."""
        self._max_retries = max_retries
        self._retry_initial_backoff = initial_backoff

    def configure_cache(
        self, *, max_entries: int = 1000, default_ttl: float = 300.0
    ) -> None:
        """Enable the LLM response cache for ``send()``."""
        from obscura.core.llm_cache import LLMCache

        self._cache = LLMCache(max_entries=max_entries, default_ttl=default_ttl)

    @property
    def circuit_registry(self) -> Any:
        """Access the circuit breaker registry (testing / admin)."""
        return self._circuit_registry

    # -- Context window / token awareness ------------------------------------

    @property
    def context_window(self) -> int:
        """Return context window size (tokens) for the active backend + model.

        Provider-specific limits per backend (tokens):
            claude   -> 200,000  (all current models)
            openai   -> 128,000  (gpt-4 family); 16,385 for gpt-3.5-turbo
            copilot  -> 128,000
            codex    -> 128,000
            *        -> 100,000  (safe unknown fallback)
        """
        _PROVIDER_DEFAULTS: dict[str, int] = {
            "claude": 200_000,
            "openai": 128_000,
            "copilot": 128_000,
            "codex": 128_000,
        }
        provider = self._backend_type.value
        model_id = self._model or ""

        # OpenAI gpt-3.5-turbo has a smaller window than the gpt-4 family
        if provider == "openai" and "3.5" in model_id:
            return 16_385

        return _PROVIDER_DEFAULTS.get(provider, 100_000)

    @property
    def context_compact_threshold(self) -> int:
        """Token count at which auto-compaction triggers (70% of context window)."""
        return int(self.context_window * 0.70)

    @property
    def context_warn_threshold(self) -> int:
        """Token count at which a soft warning is emitted (50% of context window)."""
        return int(self.context_window * 0.50)

    def _enrich_prompt(self, prompt: str) -> str:
        """Prepend relevant memory context to prompt (best-effort)."""
        if self._user is None:
            return prompt
        try:
            from obscura.memory import MemoryStore
            from obscura.auth.models import AuthenticatedUser as _AuthUser

            if isinstance(self._user, _AuthUser):
                mem = MemoryStore.for_user(self._user)
                hits = mem.search(prompt)
                if hits:
                    lines = [f"- {key}: {str(val)[:200]}" for key, val in hits[:3]]
                    ctx = "\n".join(lines)
                    return f"[Relevant context from memory]\n{ctx}\n\n{prompt}"
        except Exception:
            pass
        return prompt

    def _filter_prompt(self, prompt: str) -> str:
        """Apply prompt injection filter based on capability tier.

        Fails secure: if the filter module cannot be loaded, the prompt
        is returned unmodified only for PRIVILEGED tier.  For PUBLIC tier
        an import failure raises so callers know filtering was skipped.
        """
        if self._capability_token is None:
            return prompt
        try:
            from obscura.auth.prompt_filter import filter_prompt

            result = filter_prompt(prompt, self._capability_token.tier)
            if result.was_modified:
                _audit_prompt_filtered(self._capability_token, result.flags)
            return result.filtered
        except ImportError:
            # Fail secure: only skip filtering for privileged tier
            from obscura.auth.capability import CapabilityTier

            if self._capability_token.tier == CapabilityTier.PRIVILEGED:
                return prompt
            raise

    # -- Internals -----------------------------------------------------------

    @staticmethod
    def _resolve_model(
        backend: Backend,
        model: str | None,
        model_alias: str | None,
        automation_safe: bool,
    ) -> str | None:
        """Resolve model from alias using copilot_models, or pass through raw."""
        if model_alias is not None and backend == Backend.COPILOT:
            # Alias passthrough when copilot_models package is unavailable
            return model_alias

        # Claude aliases or raw model IDs
        if model_alias is not None and model is None:
            return model_alias

        return model

    @staticmethod
    def _create_backend(
        backend: Backend,
        auth: AuthConfig,
        model: str | None,
        system_prompt: str,
        mcp_servers: list[dict[str, Any]] | None,
        permission_mode: str,
        cwd: str | None,
        streaming: bool,
        tool_policy: ToolPolicy | None = None,
    ) -> BackendProtocol:
        """Instantiate the appropriate backend."""
        if backend == Backend.COPILOT:
            from obscura.providers.copilot import CopilotBackend

            return CopilotBackend(
                auth=auth,
                model=model,
                system_prompt=system_prompt,
                mcp_servers=mcp_servers,
                streaming=streaming,
                tool_policy=tool_policy,
            )

        if backend == Backend.CLAUDE:
            from obscura.providers.claude import ClaudeBackend

            return ClaudeBackend(
                auth=auth,
                model=model,
                system_prompt=system_prompt,
                mcp_servers=mcp_servers,
                permission_mode=permission_mode,
                cwd=cwd,
                tool_policy=tool_policy,
            )

        if backend == Backend.LOCALLLM:
            from obscura.providers.localllm import LocalLLMBackend

            return LocalLLMBackend(
                auth=auth,
                model=model,
                system_prompt=system_prompt,
                mcp_servers=mcp_servers,
            )

        if backend == Backend.OPENAI:
            from obscura.providers.openai import OpenAIBackend

            return OpenAIBackend(
                auth=auth,
                model=model,
                system_prompt=system_prompt,
                mcp_servers=mcp_servers,
            )

        if backend == Backend.CODEX:
            from obscura.providers.codex import CodexBackend

            return CodexBackend(
                auth=auth,
                model=model,
                system_prompt=system_prompt,
                mcp_servers=mcp_servers,
            )

        if backend == Backend.MOONSHOT:
            from obscura.providers.moonshot import MoonshotBackend

            return MoonshotBackend(
                auth=auth,
                model=model,
                system_prompt=system_prompt,
                mcp_servers=mcp_servers,
            )

        raise ValueError(f"Unknown backend: {backend}")


# ---------------------------------------------------------------------------
# Lazy telemetry helpers (no-op when OTel is unavailable)
# ---------------------------------------------------------------------------

from obscura.telemetry.traces import NoOpSpan, NoOpTracer


def _get_client_tracer() -> NoOpTracer:
    try:
        from obscura.telemetry.traces import get_tracer

        return get_tracer("obscura.client")
    except Exception:
        return NoOpTracer()


def _set_span_attr(span: NoOpSpan, key: str, value: Any) -> None:
    try:
        if hasattr(span, "set_attribute"):
            span.set_attribute(key, value)
    except Exception:
        pass


def _record_request_metric(backend: str, method: str, status: str) -> None:
    try:
        from obscura.telemetry.metrics import get_metrics

        m = get_metrics()
        m.requests_total.add(
            1, {"backend": backend, "method": method, "status": status}
        )
    except Exception:
        pass


def _record_request_duration(backend: str, method: str, duration: float) -> None:
    try:
        from obscura.telemetry.metrics import get_metrics

        m = get_metrics()
        m.request_duration_seconds.record(
            duration, {"backend": backend, "method": method}
        )
    except Exception:
        pass


def _record_stream_chunk(backend: str, chunk_kind: str) -> None:
    try:
        from obscura.telemetry.metrics import get_metrics

        m = get_metrics()
        m.stream_chunks_total.add(1, {"backend": backend, "chunk_kind": chunk_kind})
    except Exception:
        pass


def _record_cache_hit(backend: str) -> None:
    try:
        from obscura.telemetry.metrics import get_metrics

        get_metrics().cache_hits.add(1, {"backend": backend})
    except Exception:
        pass


def _audit_prompt_filtered(token: Any, flags: tuple[str, ...] | list[str]) -> None:
    """Emit an audit event when a prompt is modified by injection filters."""
    try:
        from obscura.telemetry.audit import AuditEvent, emit_audit_event

        emit_audit_event(
            AuditEvent(
                event_type="prompt.filtered",
                user_id=getattr(token, "user_id", "unknown"),
                user_email="",
                resource="prompt",
                action="filter",
                outcome="modified",
                details={
                    "flags": list(flags),
                    "tier": getattr(token, "tier", None)
                    and token.tier.value
                    or "unknown",
                },
            )
        )
    except Exception:
        pass
