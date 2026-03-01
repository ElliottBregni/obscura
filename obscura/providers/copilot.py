"""
obscura.copilot_backend — BackendProtocol implementation for GitHub Copilot SDK.

Wraps ``github-copilot-sdk`` (``CopilotClient``, ``CopilotSession``) behind
the unified interface. Copilot's event-push model is bridged to async iterators
via ``EventToIteratorBridge``.
"""

from __future__ import annotations

import inspect
from typing import Any, AsyncIterator, Callable, cast

from obscura.core.auth import AuthConfig
from obscura.core.sessions import SessionStore
from obscura.core.stream import EventToIteratorBridge
from obscura.core.tools import ToolRegistry
from obscura.core.tool_policy import ToolPolicy
from obscura.core.types import (
    AgentHookConfig,
    AgentEvent,
    Backend,
    BackendCapabilities,
    ChunkKind,
    ContentBlock,
    HookPoint,
    Message,
    NativeHandle,
    Role,
    SessionRef,
    StreamChunk,
    ToolChoice,
    ToolSpec,
)
from obscura.providers.registry import ModelInfo as RegistryModelInfo


# ---------------------------------------------------------------------------
# Backend implementation
# ---------------------------------------------------------------------------


class CopilotBackend:
    """BackendProtocol implementation wrapping github-copilot-sdk."""

    def __init__(
        self,
        auth: AuthConfig,
        *,
        model: str | None = None,
        system_prompt: str = "",
        mcp_servers: list[dict[str, Any]] | None = None,
        streaming: bool = True,
    tool_policy: ToolPolicy | None = None,
    ) -> None:
        self._auth = auth
        self._model = model
        self._system_prompt = system_prompt
        self._mcp_servers = mcp_servers or []
        self._streaming = streaming
        self._tool_policy = tool_policy or ToolPolicy.from_env()

        # SDK objects (set on start())
        self._client: Any = None
        self._session: Any = None

        # Tool and hook registries
        self._tools: list[ToolSpec] = []
        self._tool_registry = ToolRegistry()
        self._hooks: dict[HookPoint, list[Callable[..., Any]]] = {
            hp: [] for hp in HookPoint
        }

        # Session tracking
        self._session_store = SessionStore()

    # -- Testing/observability accessors ------------------------------------

    @property
    def model(self) -> str | None:
        return self._model

    @property
    def system_prompt(self) -> str:
        return self._system_prompt

    @property
    def streaming(self) -> bool:
        return self._streaming

    @property
    def client(self) -> Any:
        return self._client

    @property
    def session(self) -> Any:
        return self._session

    def set_client_for_testing(self, client: Any) -> None:
        self._client = client

    def set_session_for_testing(self, session: Any) -> None:
        self._session = session

    @property
    def tools(self) -> list[ToolSpec]:
        return self._tools

    @property
    def tool_registry(self) -> ToolRegistry:
        return self._tool_registry

    @property
    def hooks(self) -> dict[HookPoint, list[Callable[..., Any]]]:
        return self._hooks

    @property
    def session_store(self) -> SessionStore:
        return self._session_store

    def ensure_client_started(self) -> None:
        self._ensure_client()

    def ensure_session_started(self) -> None:
        self._ensure_session()

    def to_message(self, raw: Any) -> Message:
        return self._to_message(raw)

    @property
    def native(self) -> NativeHandle:
        """Raw SDK access for escape-hatch usage."""
        return NativeHandle(
            client=self._client,
            session=self._session,
        )

    def capabilities(self) -> BackendCapabilities:
        """Declare what this backend supports."""
        return BackendCapabilities(
            supports_streaming=True,
            supports_tool_calls=True,
            supports_tool_choice=True,
            supports_reasoning=True,
            supports_remote_sessions=True,
            supports_native_mode=True,
            native_features=(
                "event_stream",
                "sdk_sessions",
                "sdk_hooks",
                "native_client",
            ),
        )

    # -- Lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        """Initialize the Copilot client and create a default session."""
        from copilot import CopilotClient

        client_opts: dict[str, Any] = {}
        if self._auth.github_token:
            client_opts["github_token"] = self._auth.github_token
        if self._auth.byok_provider:
            client_opts["provider"] = self._auth.byok_provider

        opts: Any = client_opts if client_opts else None
        self._client = CopilotClient(opts)
        await self._client.start()

        # Create default session
        session_config = self.build_session_config()
        self._session = await self._client.create_session(session_config)

    async def stop(self) -> None:
        """Gracefully shut down the client."""
        if self._client is not None:
            await self._client.stop()
            self._client = None
            self._session = None

    # -- Send / Stream -------------------------------------------------------

    async def send(self, prompt: str, **kwargs: Any) -> Message:
        """Send a prompt and wait for the full response."""
        self._ensure_session()
        tracer = _get_backend_tracer()
        with tracer.start_as_current_span("copilot.send") as span:
            _set_span_attr(span, "backend", "copilot")
            msg_options = self._build_message_options(prompt, kwargs)
            response = await self._session.send_and_wait(msg_options)
            return self._to_message(response)

    async def stream(self, prompt: str, **kwargs: Any) -> AsyncIterator[StreamChunk]:
        """Send a prompt and yield streaming chunks."""
        self._ensure_session()
        bridge = EventToIteratorBridge()

        # Register event handlers
        unsub_fns: list[Callable[..., Any]] = []

        _got_deltas = False

        def _on_delta(event: Any) -> None:
            nonlocal _got_deltas
            _got_deltas = True
            bridge.on_text_delta(event)

        def _on_message(event: Any) -> None:
            """Fallback for full assistant messages (only if no deltas received)."""
            if _got_deltas:
                return  # Already streamed via deltas
            if (
                hasattr(event, "data")
                and hasattr(event.data, "content")
                and event.data.content
            ):
                bridge.push(
                    StreamChunk(
                        kind=ChunkKind.TEXT_DELTA, text=event.data.content, raw=event
                    )
                )

        def _on_thinking(event: Any) -> None:
            bridge.on_thinking_delta(event)

        def _on_tool_start(event: Any) -> None:
            bridge.on_tool_start(event)

        def _on_tool_end(event: Any) -> None:
            bridge.on_tool_end(event)

        def _on_idle(event: Any = None) -> None:
            bridge.finish(event)

        def _on_error(event: Any) -> None:
            bridge.error(event)

        # Subscribe to session events
        unsub_fns.append(
            self._session.on(_make_handler("assistant.message_delta", _on_delta))
        )
        unsub_fns.append(
            self._session.on(_make_handler("assistant.message", _on_message))
        )
        unsub_fns.append(
            self._session.on(_make_handler("assistant.reasoning_delta", _on_thinking))
        )
        unsub_fns.append(
            self._session.on(_make_handler("tool.execution_start", _on_tool_start))
        )
        unsub_fns.append(
            self._session.on(_make_handler("tool.execution_end", _on_tool_end))
        )
        unsub_fns.append(self._session.on(_make_handler("session.idle", _on_idle)))
        unsub_fns.append(self._session.on(_make_handler("session.error", _on_error)))

        # Send the message (non-blocking)
        tracer = _get_backend_tracer()
        span = tracer.start_span("copilot.stream")
        _set_span_attr(span, "backend", "copilot")
        try:
            yield StreamChunk(kind=ChunkKind.MESSAGE_START)

            msg_options = self._build_message_options(prompt, kwargs)
            await self._session.send(msg_options)

            # Yield chunks from the bridge
            try:
                async for chunk in bridge:
                    yield chunk
            finally:
                # Unsubscribe all handlers
                for unsub in unsub_fns:
                    if callable(unsub):
                        unsub()
        finally:
            span.end()

    # -- Sessions ------------------------------------------------------------

    async def create_session(self, **kwargs: Any) -> SessionRef:
        """Create a new named session."""
        self._ensure_client()
        config = self.build_session_config(**kwargs)
        session = await self._client.create_session(config)
        ref = SessionRef(
            session_id=session.session_id,
            backend=Backend.COPILOT,
            raw=session,
        )
        self._session_store.add(ref)
        return ref

    async def resume_session(self, ref: SessionRef) -> None:
        """Resume a previously created session."""
        self._ensure_client()
        self._session = await self._client.resume_session(ref.session_id)

    async def list_sessions(self) -> list[SessionRef]:
        """List all known sessions."""
        self._ensure_client()
        raw_sessions = await self._client.list_sessions()
        refs: list[SessionRef] = []
        for s in raw_sessions:
            ref = SessionRef(
                session_id=s.session_id if hasattr(s, "session_id") else str(s),
                backend=Backend.COPILOT,
                raw=s,
            )
            refs.append(ref)
            self._session_store.add(ref)
        return refs

    async def delete_session(self, ref: SessionRef) -> None:
        """Delete a session."""
        self._ensure_client()
        await self._client.delete_session(ref.session_id)
        self._session_store.remove(ref.session_id)

    async def fork_session(self, ref: SessionRef) -> SessionRef:
        """Fork a session.

        Uses provider-native fork APIs when available. Falls back to
        creating a new logical fork session and tracks parent metadata.
        """
        self._ensure_client()

        # Prefer explicit SDK fork support if present.
        fork_fn = getattr(self._client, "fork_session", None)
        if callable(fork_fn):
            fork_fn_typed = cast(Callable[[str], Any], fork_fn)
            session = await fork_fn_typed(ref.session_id)
            fork_ref = SessionRef(
                session_id=session.session_id,
                backend=Backend.COPILOT,
                raw=session,
            )
            self._session_store.add(fork_ref)
            self._session = session
            return fork_ref

        # Logical fork fallback: create a fresh session that records parent lineage.
        config = self.build_session_config()
        session = await self._client.create_session(config)
        fork_ref = SessionRef(
            session_id=session.session_id,
            backend=Backend.COPILOT,
            raw={"session": session, "forked_from": ref.session_id},
        )
        self._session_store.add(fork_ref)
        self._session = session
        return fork_ref

    # -- Tools ---------------------------------------------------------------

    def register_tool(self, spec: ToolSpec) -> None:
        """Register a tool for use in sessions."""
        self._tools.append(spec)
        self._tool_registry.register(spec)

    def get_tool_registry(self) -> ToolRegistry:
        """Return the tool registry for agent loop use."""
        return self._tool_registry

    # -- Hooks ---------------------------------------------------------------

    def register_hook(self, hook: HookPoint, callback: Callable[..., Any]) -> None:
        """Register a lifecycle hook callback."""
        self._hooks[hook].append(callback)

    # -- Agent loop ----------------------------------------------------------

    def run_loop(
        self,
        prompt: str,
        *,
        max_turns: int = 10,
        on_confirm: Callable[..., Any] | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[AgentEvent]:
        """Run an iterative agent loop with tool execution.

        Yields ``AgentEvent`` instances as the model streams text,
        calls tools, and iterates across multiple turns.

        Example::

            async for event in backend.run_loop("Fix the bug", max_turns=5):
                print(event)
        """
        from obscura.core.agent_loop import AgentLoop

        loop = AgentLoop(
            self,
            self._tool_registry,
            max_turns=max_turns,
            on_confirm=on_confirm,
        )
        return loop.run(prompt, **kwargs)

    # -- Internals -----------------------------------------------------------


    # -- Provider Registry (model discovery) ---------------------------------

    async def list_models(self) -> list[RegistryModelInfo]:
        """List models available from Copilot (hybrid approach)."""
        try:
            # Try to use copilot_models package if available
            from copilot_models import COPILOT_MODELS
            return [
                RegistryModelInfo(
                    id=model["id"],
                    name=model.get("name", model["id"]),
                    provider="copilot",
                    supports_tools=model.get("supports_tools", True),
                    supports_vision=model.get("supports_vision", False),
                )
                for model in COPILOT_MODELS
            ]
        except ImportError:
            # Fallback to known models
            return self._get_fallback_models()

    def get_default_model(self) -> str:
        """Return the default model for this provider."""
        return "auto"  # Copilot chooses best model

    def validate_model(self, model_id: str) -> bool:
        """Check if a model ID is valid for Copilot."""
        return True  # Copilot validates internally

    def _get_fallback_models(self) -> list[RegistryModelInfo]:
        """Fallback list when copilot_models package unavailable."""
        return [
            RegistryModelInfo(
                id="auto",
                name="Copilot Auto (Best Available)",
                provider="copilot",
                supports_tools=True,
                supports_vision=True,
            ),
        ]

    def _ensure_client(self) -> None:
        if self._client is None:
            raise RuntimeError("CopilotBackend not started. Call start() first.")

    def _ensure_session(self) -> None:
        self._ensure_client()
        if self._session is None:
            raise RuntimeError(
                "No active session. Call start() or create_session() first."
            )

    def build_session_config(self, **overrides: Any) -> dict[str, Any]:
        """Build a SessionConfig dict for the Copilot SDK."""
        config: dict[str, Any] = {}

        if self._model:
            config["model"] = self._model
        if self._system_prompt:
            config["system_message"] = {
                "mode": "append",
                "content": self._system_prompt,
            }
        if self._streaming:
            config["streaming"] = True
        if self._mcp_servers:
            config["mcp_servers"] = self._mcp_servers
        if self._tools:
            config["tools"] = self._tools
            # Apply tool policy to restrict native tools
            self._tool_policy.apply_to_copilot(config, self._tools)

        # Apply hook mappings
        hooks = self.build_hooks_config()
        if hooks:
            config["hooks"] = hooks

        config.update(overrides)
        return config

    def _build_message_options(
        self, prompt: str, kwargs: dict[str, Any]
    ) -> dict[str, Any]:
        """Build per-message options including normalized tool choice."""
        msg_options: dict[str, Any] = {"prompt": prompt}
        options = kwargs.get("options")
        if isinstance(options, dict):
            msg_options.update(cast(dict[str, Any], options))

        tool_choice = kwargs.get("tool_choice")
        if isinstance(tool_choice, ToolChoice):
            msg_options["tool_choice"] = self._convert_tool_choice(tool_choice)
        elif tool_choice is not None:
            msg_options["tool_choice"] = tool_choice
        return msg_options

    @staticmethod
    def _convert_tool_choice(choice: ToolChoice) -> Any:
        """Convert unified ToolChoice to a provider-neutral dict payload."""
        if choice.mode == "auto":
            return {"mode": "auto"}
        if choice.mode == "none":
            return {"mode": "none"}
        if choice.mode == "required":
            return {"mode": "required"}
        if choice.mode == "function":
            return {"mode": "function", "name": choice.function_name}
        return {"mode": "auto"}

    def build_hooks_config(self) -> dict[str, Any] | None:
        """Translate registered hooks to Copilot SDK hook config."""
        hook_map: dict[str, str] = {
            HookPoint.PRE_TOOL_USE.value: "on_pre_tool_use",
            HookPoint.POST_TOOL_USE.value: "on_post_tool_use",
            HookPoint.USER_PROMPT_SUBMITTED.value: "on_user_prompt_submitted",
            HookPoint.STOP.value: "on_stop",
        }

        result: AgentHookConfig = {}
        for hp, callbacks in self._hooks.items():
            if not callbacks:
                continue
            copilot_key = hook_map.get(hp.value)
            if not copilot_key:
                continue
            if len(callbacks) == 1:
                result[copilot_key] = callbacks[0]
            else:

                async def _chained(
                    *args: Any, cbs: list[Callable[..., Any]] = callbacks, **kw: Any
                ) -> Any:
                    last_result: Any = None
                    for cb in cbs:
                        last_result = (
                            await cb(*args, **kw)
                            if inspect.iscoroutinefunction(cb)
                            else cb(*args, **kw)
                        )
                    return last_result

                result[copilot_key] = _chained

        return result or None

    def _to_message(self, raw: Any) -> Message:
        """Convert a Copilot response to a normalized Message."""
        # Extract text content from response
        text = ""
        if hasattr(raw, "data") and hasattr(raw.data, "content"):
            text = raw.data.content
        elif hasattr(raw, "content"):
            text = raw.content
        elif isinstance(raw, str):
            text = raw
        else:
            text = str(raw)

        return Message(
            role=Role.ASSISTANT,
            content=[ContentBlock(kind="text", text=text)],
            raw=raw,
            backend=Backend.COPILOT,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_handler(event_type: str, callback: Callable[..., Any]) -> Callable[..., Any]:
    """Create a Copilot event handler that filters by event type.

    Copilot's session.on() passes ALL events to every handler.
    This wrapper filters so the callback only fires for matching types.
    """

    def handler(event: Any) -> None:
        if hasattr(event, "type"):
            etype = event.type
            # Compare against string directly or via enum .value
            if etype == event_type or getattr(etype, "value", None) == event_type:
                callback(event)
        # If event has no type field, silently ignore (don't call through)

    # Tag the handler so the SDK can identify it
    setattr(handler, "_event_type", event_type)
    return handler


# Export helper for tests
public_make_handler = _make_handler


# ---------------------------------------------------------------------------
# Lazy telemetry helpers
# ---------------------------------------------------------------------------

from obscura.telemetry.traces import NoOpTracer


def _get_backend_tracer() -> Any:
    try:
        from obscura.telemetry.traces import get_tracer

        return get_tracer("obscura.copilot_backend")
    except Exception:
        return NoOpTracer()


def _set_span_attr(span: Any, key: str, value: Any) -> None:
    try:
        if hasattr(span, "set_attribute"):
            span.set_attribute(key, value)
    except Exception:
        pass


# Export telemetry helpers for tests
public_get_backend_tracer = _get_backend_tracer
public_set_span_attr = _set_span_attr
