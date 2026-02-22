"""
obscura.client — ObscuraClient: unified entry point for all backends.

Dispatches to the appropriate backend (Copilot, Claude, OpenAI, Moonshot, LocalLLM)
based on the ``backend`` parameter. Integrates with ``copilot_models`` for
model alias resolution and safety guards.
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Awaitable, Callable, cast

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
        # Claude-specific
        permission_mode: str = "default",
        cwd: str | None = None,
        # Copilot-specific
        streaming: bool = True,
        # HTTP server context
        user: object | None = None,
    ) -> None:
        if isinstance(backend, str):
            backend = Backend(backend)
        self._backend_type = backend
        self._user = user

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
                    # Inject tier-appropriate system prompt
                    system_prompt = get_tier_system_prompt(
                        self._capability_token.tier,
                        additional=system_prompt,
                    )
            except Exception:
                pass  # Degrade gracefully if capability module not available

        # Create backend
        self._backend = self._create_backend(
            backend=backend,
            auth=resolved_auth,
            model=resolved_model,
            system_prompt=system_prompt,
            mcp_servers=mcp_servers,
            permission_mode=permission_mode,
            cwd=cwd,
            streaming=streaming,
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
        """Initialize the backend connection."""
        await self._backend.start()

    async def stop(self) -> None:
        """Gracefully shut down."""
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

        # Apply prompt injection filter
        prompt = self._filter_prompt(prompt)

        tracer = _get_client_tracer()
        with tracer.start_as_current_span("obscura.core.client.send") as span:
            _set_span_attr(span, "obscura.backend", self._backend_type.value)
            _set_span_attr(span, "obscura.method", "send")
            start = _time.monotonic()
            try:
                result = await self._backend.send(prompt, **kwargs)
                duration = _time.monotonic() - start
                _record_request_metric(self._backend_type.value, "send", "success")
                _record_request_duration(self._backend_type.value, "send", duration)
                return result
            except Exception:
                duration = _time.monotonic() - start
                _record_request_metric(self._backend_type.value, "send", "error")
                _record_request_duration(self._backend_type.value, "send", duration)
                raise

    async def stream(self, prompt: str, **kwargs: Any) -> AsyncIterator[StreamChunk]:
        """Send prompt, yield streaming chunks."""
        import time as _time

        # Apply prompt injection filter
        prompt = self._filter_prompt(prompt)

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
        except Exception:
            status = "error"
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
        """
        from obscura.core.agent_loop import AgentLoop

        loop = AgentLoop(
            self._backend,
            self._tool_registry,
            max_turns=max_turns,
            on_confirm=on_confirm,
            capability_token=self._capability_token,
        )
        return loop.run(prompt, **kwargs)

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

        loop = AgentLoop(
            self._backend,
            self._tool_registry,
            max_turns=max_turns,
            on_confirm=on_confirm,
            capability_token=self._capability_token,
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
