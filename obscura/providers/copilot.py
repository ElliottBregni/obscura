"""obscura.copilot_backend — BackendProtocol implementation for GitHub Copilot SDK.

Wraps ``github-copilot-sdk`` (``CopilotClient``, ``CopilotSession``) behind
the unified interface. Copilot's event-push model is bridged to async iterators
via ``EventToIteratorBridge``.
"""

from __future__ import annotations

import inspect
import logging
from typing import TYPE_CHECKING, Any, cast

from obscura.core.sessions import SessionStore
from obscura.core.stream import EventToIteratorBridge
from obscura.core.tool_policy import ToolPolicy
from obscura.core.tools import ToolRegistry
from obscura.core.types import (
    AgentEvent,
    AgentHookConfig,
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

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from copilot.generated.session_events import PermissionRequest

    from obscura.core.auth import AuthConfig
    from obscura.core.tool_router import RoutingResult

# ---------------------------------------------------------------------------
# Priority-aware tool truncation
# ---------------------------------------------------------------------------

# Core tools that must never be dropped by naive truncation.
# Mirrors DEFAULT_PINNED_TOOLS in tool_router.py.
_CORE_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "run_shell",
        "read_text_file",
        "write_text_file",
        "edit_text_file",
        "list_directory",
        "grep_files",
        "find_files",
        "git",
    },
)


def _priority_truncate(tools: list[ToolSpec], limit: int) -> list[ToolSpec]:
    """Truncate *tools* to *limit*, keeping core tools and dropping plugins first.

    Tiers (kept in order):
      1. Core system tools (always kept)
      2. MCP tools that match core names (e.g. ``mcp__obscura_tools__run_shell``)
      3. Non-MCP tools (native plugins)
      4. Remaining MCP tools (plugin MCP tools — dropped first)
    """
    core: list[ToolSpec] = []
    mcp_core: list[ToolSpec] = []
    native: list[ToolSpec] = []
    mcp_other: list[ToolSpec] = []

    for t in tools:
        name = t.name
        if name in _CORE_TOOL_NAMES:
            core.append(t)
        elif name.startswith("mcp__"):
            # Check if the MCP tool name ends with a core tool name
            suffix = name.rsplit("__", 1)[-1] if "__" in name else ""
            if suffix in _CORE_TOOL_NAMES:
                mcp_core.append(t)
            else:
                mcp_other.append(t)
        else:
            native.append(t)

    # Assemble in priority order: core first, MCP plugins last
    prioritised = core + mcp_core + native + mcp_other
    return prioritised[:limit]


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
        self._model = model or "gpt-5-mini"
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

        # Tool routing
        self._tool_router: Any | None = None

        # Session tracking
        self._session_store = SessionStore()
        self._log = logging.getLogger(__name__)

    # -- Tool routing --------------------------------------------------------

    def set_tool_router(self, router: Any) -> None:
        """Attach a :class:`ToolRouter` for per-turn tool selection."""
        self._tool_router = router

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
        from copilot import CopilotClient, SubprocessConfig

        client_opts: Any = None
        if self._auth.github_token:
            client_opts = SubprocessConfig(github_token=self._auth.github_token)

        self._client = CopilotClient(client_opts)
        await self._client.start()

        # Create default session
        session_config = self.build_session_config()

        self._session = await self._client.create_session(**session_config)

    async def reset_session(self) -> None:
        """Create a fresh session, discarding prior conversation state.

        Needed when the session's event state machine gets stuck after
        a completed or timed-out stream() call.
        """
        self._ensure_client()
        config = self.build_session_config()
        self._session = await self._client.create_session(**config)

    async def stop(self) -> None:
        """Gracefully shut down the client."""
        if self._client is not None:
            await self._client.stop()
            self._client = None
            self._session = None

    # -- Send / Stream -------------------------------------------------------

    @staticmethod
    def _is_session_expired(exc: Exception) -> bool:
        """Check if an exception indicates the server-side session expired."""
        msg = str(exc).lower()
        return "session not found" in msg or (
            "json-rpc error" in msg and "session" in msg
        )

    async def _recover_session(self) -> None:
        """Recreate the session after an expiry/idle timeout."""
        self._log.warning("Session expired after idle — recreating session")
        config = self.build_session_config()
        self._session = await self._client.create_session(**config)

    async def send(self, prompt: str, **kwargs: Any) -> Message:
        """Send a prompt and wait for the full response."""
        self._ensure_session()
        tracer = _get_backend_tracer()
        with tracer.start_as_current_span("copilot.send") as span:
            _set_span_attr(span, "backend", "copilot")
            msg_options = self._build_message_options(prompt, kwargs)
            send_prompt = msg_options.pop("prompt")
            try:
                response = await self._session.send_and_wait(send_prompt, **msg_options)
            except Exception as exc:
                if not self._is_session_expired(exc):
                    raise
                await self._recover_session()
                msg_options = self._build_message_options(prompt, kwargs)
                send_prompt = msg_options.pop("prompt")
                response = await self._session.send_and_wait(send_prompt, **msg_options)
            return self._to_message(response)

    async def stream(self, prompt: str, **kwargs: Any) -> AsyncIterator[StreamChunk]:
        """Send a prompt and yield streaming chunks.

        Automatically recovers from expired sessions (idle timeout).
        """
        self._ensure_session()
        try:
            async for chunk in self._do_stream(prompt, **kwargs):
                yield chunk
        except Exception as exc:
            if not self._is_session_expired(exc):
                raise
            await self._recover_session()
            async for chunk in self._do_stream(prompt, **kwargs):
                yield chunk

    async def _do_stream(
        self,
        prompt: str,
        **kwargs: Any,
    ) -> AsyncIterator[StreamChunk]:
        """Core streaming implementation."""
        bridge = EventToIteratorBridge()
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
                        kind=ChunkKind.TEXT_DELTA,
                        text=event.data.content,
                        raw=event,
                    ),
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
            # Enrich the error chunk with runtime context so downstream
            # consumers (event store, supervisor) see session/model info
            # instead of all-None fields.
            from obscura.core.types import StreamMetadata

            session_id = ""
            if self._session and hasattr(self._session, "session_id"):
                session_id = str(self._session.session_id)
            meta = StreamMetadata(
                model_id=self._model or "",
                session_id=session_id,
            )
            bridge.error(event, metadata=meta)

        # Subscribe to session events
        unsub_fns.append(
            self._session.on(_make_handler("assistant.message_delta", _on_delta)),
        )
        unsub_fns.append(
            self._session.on(_make_handler("assistant.message", _on_message)),
        )
        unsub_fns.append(
            self._session.on(_make_handler("assistant.reasoning_delta", _on_thinking)),
        )
        unsub_fns.append(
            self._session.on(_make_handler("tool.execution_start", _on_tool_start)),
        )
        unsub_fns.append(
            self._session.on(_make_handler("tool.execution_end", _on_tool_end)),
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
            send_prompt = msg_options.pop("prompt")
            await self._session.send(send_prompt, **msg_options)

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
        session = await self._client.create_session(**config)
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
        config = self.build_session_config()
        session_id = config.pop("session_id", None) or ref.session_id
        self._session = await self._client.resume_session(session_id, **config)

    async def list_sessions(self) -> list[SessionRef]:
        """List all known sessions."""
        self._ensure_client()
        raw_sessions = await self._client.list_sessions()
        refs: list[SessionRef] = []
        for s in raw_sessions:
            sid = getattr(s, "sessionId", None) or getattr(s, "session_id", str(s))
            ref = SessionRef(
                session_id=str(sid),
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
            fork_fn_typed = cast("Callable[[str], Any]", fork_fn)
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
        session = await self._client.create_session(**config)
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
        """Register a tool for use in sessions (skips duplicates)."""
        if any(t.name == spec.name for t in self._tools):
            return
        self._tools.append(spec)
        self._tool_registry.register(spec)

    def get_tool_registry(self) -> ToolRegistry:
        """Return the tool registry for agent loop use."""
        return self._tool_registry

    def _build_tool_listing(self) -> str:
        """Build a human-readable tool listing for the system prompt."""
        lines = ["## Available Tools", ""]
        lines.append(
            "You have the following tools. Use these EXACT names when calling tools:",
        )
        lines.append("")
        for spec in self._tools:
            desc = (spec.description or "").split("\n")[0][:120]
            cap_tag = f" [{spec.capability}]" if getattr(spec, "capability", "") else ""
            lines.append(f"- `{self._sanitize_tool_name(spec.name)}`{cap_tag}: {desc}")
        lines.append("")
        lines.append(
            "Do NOT invent tool names. If none of these tools fit, tell the user.",
        )
        try:
            from obscura.plugins.capabilities import build_capability_map_section

            cap_section = build_capability_map_section(self._tools)
            if cap_section:
                lines.append("")
                lines.append(cap_section)
        except Exception:
            pass
        return "\n".join(lines)

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
        # Use fallback only for now TODO: Verify we can pull model info from.. somewhere
        return self._get_fallback_models()

    def get_default_model(self) -> str:
        """Return the default model for this provider."""
        return "gpt-5-mini"

    def validate_model(self, model_id: str) -> bool:
        """Check if a model ID is valid for Copilot."""
        return True  # Copilot validates internally

    def _get_fallback_models(self) -> list[RegistryModelInfo]:
        """Fallback list when copilot_models package unavailable."""
        return [
            RegistryModelInfo(
                id="gpt-5-mini",
                name="gpt-5-mini",
                provider="copilot",
                supports_tools=True,
                supports_vision=True,
            ),
        ]

    def _ensure_client(self) -> None:
        if self._client is None:
            msg = "CopilotBackend not started. Call start() first."
            raise RuntimeError(msg)

    def _ensure_session(self) -> None:
        self._ensure_client()
        if self._session is None:
            msg = "No active session. Call start() or create_session() first."
            raise RuntimeError(
                msg,
            )

    @staticmethod
    def _sanitize_tool_name(name: str) -> str:
        """Sanitize tool name to match API pattern ^[a-zA-Z0-9_-]{1,128}$."""
        import re

        return re.sub(r"[^a-zA-Z0-9_-]", "_", name)[:128]

    def _convert_tools_to_copilot(self, tools: list[ToolSpec]) -> list[Any]:
        """Convert Obscura ToolSpec objects to Copilot SDK Tool format.

        The Copilot SDK expects ``copilot.types.Tool`` objects whose handler
        matches ``Callable[[ToolInvocation], ToolResult | Awaitable[ToolResult]]``.
        This method wraps each ``ToolSpec.handler`` so it accepts a
        ``ToolInvocation`` dict and returns a ``ToolResult`` TypedDict.

        Tool names are sanitized to match the model API's required pattern
        ``^[a-zA-Z0-9_-]{1,128}$`` (dots and other special chars replaced
        with underscores).
        """
        from copilot.client import Tool

        converted: list[Any] = []
        for spec in tools:
            _handler = spec.handler

            def _wrapper_factory(handler: Callable[..., Any]) -> Callable[..., Any]:
                async def wrapped(invocation: Any) -> Any:
                    import inspect as _inspect

                    from copilot.client import ToolResult as CopilotToolResult

                    try:
                        raw_args = invocation.arguments
                        args = cast("dict[str, Any]", raw_args) if raw_args else {}
                        result: Any = handler(**args)
                        if _inspect.isawaitable(result):
                            result = await result
                        # Normalize to SDK ToolResult (snake_case fields in 0.2.0)
                        if isinstance(result, CopilotToolResult):
                            return result
                        text = str(result) if result is not None else ""
                        return CopilotToolResult(
                            text_result_for_llm=text,
                            result_type="success",
                        )
                    except Exception as exc:
                        return CopilotToolResult(
                            text_result_for_llm=f"Tool error: {exc}",
                            result_type="failure",
                            error=str(exc),
                        )

                return wrapped

            # Ensure parameters is always a valid JSON Schema object —
            # the Copilot SDK crashes with .map() on undefined if None.
            params: dict[str, Any] = spec.parameters or {
                "type": "object",
                "properties": {},
            }
            converted.append(
                Tool(
                    name=self._sanitize_tool_name(spec.name),
                    description=spec.description,
                    handler=_wrapper_factory(_handler),
                    parameters=params,
                    overrides_built_in_tool=True,
                ),
            )
        return converted

    def build_session_config(self, **overrides: Any) -> dict[str, Any]:
        """Build a SessionConfig dict for the Copilot SDK."""
        import logging

        _log = logging.getLogger(__name__)
        config: dict[str, Any] = {}

        from copilot.session import PermissionRequestResult

        def _approve_all(
            request: PermissionRequest,
            _context: dict[str, str],
        ) -> PermissionRequestResult:
            return PermissionRequestResult(kind="approved")

        config["on_permission_request"] = _approve_all

        # BYOK provider config (moved from client opts → session config in SDK 0.2.0)
        if self._auth.byok_provider:
            config["provider"] = self._auth.byok_provider

        if self._model:
            config["model"] = self._model
        prompt = self._system_prompt or ""
        if self._tools:
            prompt = (
                f"{prompt}\n\n{self._build_tool_listing()}"
                if prompt
                else self._build_tool_listing()
            )
        if prompt:
            config["system_message"] = {
                "mode": "append",
                "content": prompt,
            }
        if self._streaming:
            config["streaming"] = True
        if self._mcp_servers:
            # SDK 0.2.0 changed mcp_servers from list[dict] to dict[str, MCPServerConfig]
            mcp_dict: dict[str, Any] = {}
            for srv in self._mcp_servers:
                name = srv.get("name", f"mcp-{len(mcp_dict)}")
                mcp_dict[name] = {k: v for k, v in srv.items() if k != "name"}
            config["mcp_servers"] = mcp_dict
        if self._tools:
            # Filter tools first via policy
            filtered = self._tool_policy.filter_tools(self._tools)

            # Apply eval-driven tool routing if a router is configured.
            if self._tool_router is not None:
                result: RoutingResult = self._tool_router.select(prompt, filtered)
                filtered = result.tools
                if result.dropped_count > 0:
                    _log.info(
                        "Tool router: %d/%d (pinned=%d, cap=%d, scored=%d, quarantined=%d)",
                        len(filtered),
                        len(filtered) + result.dropped_count,
                        len(result.pinned),
                        len(result.capability_matched),
                        len(result.score_ranked),
                        result.quarantined_count,
                    )

            # Safety-net hard cap — should rarely trigger with a router.
            _MAX_COPILOT_TOOLS = 128
            if len(filtered) > _MAX_COPILOT_TOOLS:
                _log.warning(
                    "Copilot tool cap: %d tools exceeds limit of %d — truncating",
                    len(filtered),
                    _MAX_COPILOT_TOOLS,
                )
                filtered = _priority_truncate(filtered, _MAX_COPILOT_TOOLS)

            _log.debug(
                "Building session with %d tools (system prompt %d chars)",
                len(filtered),
                len(prompt),
            )
            # Convert to Copilot SDK Tool format
            config["tools"] = self._convert_tools_to_copilot(filtered)
            # Use correct SessionConfig field name: available_tools (not allowed_tools)
            if not self._tool_policy.allow_native:
                config["available_tools"] = [
                    self._sanitize_tool_name(t.name) for t in filtered
                ]

        # Apply hook mappings
        hooks = self.build_hooks_config()
        if hooks:
            config["hooks"] = hooks

        config.update(overrides)
        return config

    # Copilot API rejects request bodies that exceed its internal limit.
    # Truncate prompts to stay safely under the wire.
    _MAX_PROMPT_CHARS = 120_000

    def _truncate_prompt(self, prompt: str) -> str:
        """Truncate prompt to stay within Copilot API body-size limits."""
        if len(prompt) <= self._MAX_PROMPT_CHARS:
            return prompt
        self._log.warning(
            "Prompt truncated: %d chars → %d chars",
            len(prompt),
            self._MAX_PROMPT_CHARS,
        )
        truncated = prompt[: self._MAX_PROMPT_CHARS]
        # Avoid cutting mid-word
        last_space = truncated.rfind(" ", self._MAX_PROMPT_CHARS - 200)
        if last_space > 0:
            truncated = truncated[:last_space]
        return truncated + "\n\n[… content truncated due to length]"

    # SDK 0.2.0 send/send_and_wait accept only these kwargs.
    _SEND_ALLOWED_KEYS = {"prompt", "attachments", "mode"}
    _SEND_AND_WAIT_ALLOWED_KEYS = {"prompt", "attachments", "mode", "timeout"}

    def set_thinking_budget(self, tokens: int | None) -> None:
        """No-op: Copilot backend does not support extended thinking budgets."""
        import logging as _logging

        _logging.getLogger(__name__).debug(
            "set_thinking_budget(%s) ignored — Copilot backend does not support extended thinking.",
            tokens,
        )

    def _build_message_options(
        self,
        prompt: str,
        kwargs: dict[str, Any],
    ) -> dict[str, Any]:
        """Build per-message keyword args for session.send / send_and_wait.

        Returns a dict suitable for ``**``-unpacking into the SDK methods.
        The ``prompt`` key is always present as a positional-ready value.
        Keys not accepted by the SDK (e.g. ``tool_choice``, ``max_thinking_tokens``) are dropped.
        """
        # Drain effort/thinking keys — Copilot SDK does not support them.
        # We do this explicitly so callers don't see silent drops or type errors.
        kwargs.pop("max_thinking_tokens", None)

        msg_options: dict[str, Any] = {"prompt": self._truncate_prompt(prompt)}
        options = kwargs.get("options")
        if isinstance(options, dict):
            msg_options.update(cast("dict[str, Any]", options))
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
                    *args: Any,
                    cbs: list[Callable[..., Any]] = callbacks,
                    **kw: Any,
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
    handler._event_type = event_type
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
