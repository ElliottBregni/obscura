"""
obscura.claude_backend — BackendProtocol implementation for Claude Agent SDK.

Wraps ``claude-agent-sdk`` (``ClaudeSDKClient``, ``query()``) behind the
unified interface. Claude's async-iterator model maps naturally to our
``stream()`` method; ``send()`` simply collects the iterator.
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Callable, cast
import re

from obscura.core.auth import AuthConfig
from obscura.core.sessions import SessionStore
from obscura.core.stream import ClaudeIteratorAdapter
from obscura.core.tools import ToolRegistry
from obscura.core.tool_policy import ToolPolicy
from obscura.core.types import (
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


class ClaudeBackend:
    """BackendProtocol implementation wrapping claude-agent-sdk."""

    def __init__(
        self,
        auth: AuthConfig,
        *,
        model: str | None = None,
        system_prompt: str = "",
        mcp_servers: list[dict[str, Any]] | None = None,
        permission_mode: str = "default",
        cwd: str | None = None,
    tool_policy: ToolPolicy | None = None,
    ) -> None:
        self._auth = auth
        self._model = model or "claude-sonnet-4-6"
        self._system_prompt = system_prompt
        self._mcp_servers = mcp_servers or []
        self._permission_mode = permission_mode
        self._cwd = cwd
        self._tool_policy = tool_policy or ToolPolicy.from_env()

        # SDK objects (set on start())
        self._client: Any = None
        self._last_session_id: str | None = None

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
    def model(self) -> str:
        return self._model

    @property
    def system_prompt(self) -> str:
        return self._system_prompt

    @property
    def permission_mode(self) -> str:
        return self._permission_mode

    @property
    def cwd(self) -> str | None:
        return self._cwd

    @property
    def client(self) -> Any:
        return self._client

    def set_client_for_testing(self, client: Any) -> None:
        """Inject a fake ClaudeSDKClient for tests."""
        self._client = client

    @property
    def tools(self) -> list[ToolSpec]:
        return self._tools

    @property
    def hooks(self) -> dict[HookPoint, list[Callable[..., Any]]]:
        return self._hooks

    @property
    def session_store(self) -> SessionStore:
        return self._session_store

    def ensure_client_started(self) -> None:
        """Public wrapper used in tests."""
        self._ensure_client()

    def to_message(self, raw_messages: list[Any]) -> Message:
        """Public wrapper for testing message conversion."""
        return self._to_message(raw_messages)

    @property
    def native(self) -> NativeHandle:
        """Raw SDK access for escape-hatch usage."""
        return NativeHandle(
            client=self._client,
            meta={"last_session_id": self._last_session_id},
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
                "permission_modes",
                "session_resume",
                "session_fork",
                "mcp_inprocess",
                "native_client",
            ),
        )

    # -- Confirmation gate (PreToolUse hook) ----------------------------------

    def enable_confirmation(
        self, confirm_fn: Callable[[str, dict[str, Any]], bool]
    ) -> None:
        """Register a PreToolUse hook that gates tool calls on user approval.

        Because Claude SDK executes tools internally via MCP, the normal
        ``AgentLoop.on_confirm`` callback is never reached.  This method
        installs a ``PreToolUse`` hook that intercepts tool calls inside the
        SDK and prompts the user through *confirm_fn*.

        Parameters
        ----------
        confirm_fn:
            ``(tool_name, tool_input) -> bool``.  Return True to allow.
        """

        def _pre_tool_hook(
            tool_name: str = "",
            tool_input: dict[str, Any] | None = None,
            **kw: Any,
        ) -> dict[str, Any] | None:
            if not confirm_fn(tool_name, tool_input or {}):
                return {"denied": True, "reason": "Tool call denied by user."}
            return None  # allow

        self._hooks.setdefault(HookPoint.PRE_TOOL_USE, []).append(
            _pre_tool_hook,
        )

    # -- Lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        """Initialize the Claude SDK client."""
        from claude_agent_sdk import ClaudeSDKClient

        options = self._build_options()
        self._client = ClaudeSDKClient(options=options)
        await self._client.connect()

    async def stop(self) -> None:
        """Disconnect from Claude."""
        if self._client is not None:
            await self._client.disconnect()
            self._client = None

    # -- Send / Stream -------------------------------------------------------

    async def send(self, prompt: str, **kwargs: Any) -> Message:
        """Send a prompt and wait for the full response."""
        self._ensure_client()
        tracer = _get_backend_tracer()
        with tracer.start_as_current_span("claude.send") as span:
            _set_span_attr(span, "backend", "claude")

            # Drain any pending response before querying
            async for _ in self._client.receive_response():
                pass

            # Use query for a fresh exchange
            await self._query(prompt, kwargs)
            messages: list[Any] = []
            async for msg in self._client.receive_response():
                messages.append(msg)
                # Track session ID from ResultMessage
                if type(msg).__name__ == "ResultMessage" and hasattr(msg, "session_id"):
                    self._last_session_id = msg.session_id

            return self._to_message(messages)

    async def stream(self, prompt: str, **kwargs: Any) -> AsyncIterator[StreamChunk]:
        """Send a prompt and yield streaming chunks."""
        self._ensure_client()
        tracer = _get_backend_tracer()
        span = tracer.start_span("claude.stream")
        _set_span_attr(span, "backend", "claude")
        try:
            await self._query(prompt, kwargs)
            source = self._client.receive_response()
            adapter = ClaudeIteratorAdapter(source)

            async for chunk in adapter:
                # Track session ID from done events
                if chunk.kind == ChunkKind.DONE and chunk.raw is not None:
                    if hasattr(chunk.raw, "session_id"):
                        self._last_session_id = chunk.raw.session_id
                yield chunk
        finally:
            span.end()

    # -- Sessions ------------------------------------------------------------

    async def create_session(self, **kwargs: Any) -> SessionRef:
        """Create a new session (starts a fresh query context)."""
        self._ensure_client()

        # Claude sessions are implicit — each query sequence is a session.
        # We track the session_id returned in ResultMessage.
        # For explicit session management, we can use resume= option.
        await self._client.query(kwargs.get("prompt", ""))
        async for msg in self._client.receive_response():
            if type(msg).__name__ == "ResultMessage" and hasattr(msg, "session_id"):
                self._last_session_id = msg.session_id

        if self._last_session_id:
            ref = SessionRef(
                session_id=self._last_session_id,
                backend=Backend.CLAUDE,
            )
            self._session_store.add(ref)
            return ref

        raise RuntimeError("Failed to obtain session ID from Claude.")

    async def resume_session(self, ref: SessionRef) -> None:
        """Resume a previous session by reconnecting with the session ID."""
        if self._client is not None:
            await self._client.disconnect()

        from claude_agent_sdk import ClaudeSDKClient

        options = self._build_options(resume=ref.session_id)
        self._client = ClaudeSDKClient(options=options)
        await self._client.connect()
        self._last_session_id = ref.session_id

    async def list_sessions(self) -> list[SessionRef]:
        """List tracked sessions (Claude doesn't have a native list API)."""
        return self._session_store.list_all(Backend.CLAUDE)

    async def delete_session(self, ref: SessionRef) -> None:
        """Remove a session from tracking."""
        self._session_store.remove(ref.session_id)

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

    # -- Hooks ---------------------------------------------------------------

    def register_hook(self, hook: HookPoint, callback: Callable[..., Any]) -> None:
        """Register a lifecycle hook callback."""
        self._hooks[hook].append(callback)

    # -- Claude-specific methods (escape hatch) ------------------------------

    async def fork_session(self, ref: SessionRef) -> SessionRef:
        """Fork a session (Claude-specific feature)."""
        if self._client is not None:
            await self._client.disconnect()

        from claude_agent_sdk import ClaudeSDKClient

        options = self._build_options(resume=ref.session_id, fork_session=True)
        self._client = ClaudeSDKClient(options=options)
        await self._client.connect()

        # The new session ID will come from the next ResultMessage
        return ref  # Caller should send a message to get the new session ID


    # -- Provider Registry (model discovery) ---------------------------------

    async def list_models(self) -> list[RegistryModelInfo]:
        """List models available from Claude SDK catalog."""
        return [
            RegistryModelInfo(
                id="claude-opus-4-6",
                name="Claude Opus 4.6",
                provider="claude",
                context_window=200000,
                max_output_tokens=32000,
                supports_tools=True,
                supports_vision=True,
            ),
            RegistryModelInfo(
                id="claude-sonnet-4-6",
                name="Claude Sonnet 4.6",
                provider="claude",
                context_window=200000,
                max_output_tokens=16000,
                supports_tools=True,
                supports_vision=True,
            ),
            RegistryModelInfo(
                id="claude-haiku-4-5-20251001",
                name="Claude Haiku 4.5",
                provider="claude",
                context_window=200000,
                max_output_tokens=8192,
                supports_tools=True,
                supports_vision=True,
            ),
            RegistryModelInfo(
                id="claude-sonnet-4-5-20250929",
                name="Claude Sonnet 4.5",
                provider="claude",
                context_window=200000,
                max_output_tokens=8192,
                supports_tools=True,
                supports_vision=True,
                deprecated=True,
            ),
            RegistryModelInfo(
                id="claude-opus-4-20250514",
                name="Claude Opus 4",
                provider="claude",
                context_window=200000,
                max_output_tokens=4096,
                supports_tools=True,
                supports_vision=True,
                deprecated=True,
            ),
        ]

    def get_default_model(self) -> str:
        """Return the default model for this provider."""
        return "claude-sonnet-4-6"

    def validate_model(self, model_id: str) -> bool:
        """Check if a model ID is valid for Claude."""
        return model_id.startswith('claude-')

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

    def _ensure_client(self) -> None:
        if self._client is None:
            raise RuntimeError("ClaudeBackend not started. Call start() first.")

    async def _query(self, prompt: str, kwargs: dict[str, Any]) -> None:
        """Issue a Claude query with optional per-request tool policy."""
        query_kwargs = self._build_query_kwargs(kwargs)
        if query_kwargs:
            try:
                await self._client.query(prompt, **query_kwargs)
                return
            except TypeError:
                # Older SDKs may not accept query kwargs.
                pass
        await self._client.query(prompt)

    def _build_query_kwargs(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        """Convert unified kwargs into Claude query kwargs."""
        tool_choice = kwargs.get("tool_choice")
        return self._convert_tool_choice(tool_choice)

    def _convert_tool_choice(self, choice: Any) -> dict[str, Any]:
        """Map ToolChoice to Claude query kwargs."""
        if choice is None:
            return {}

        tool_names = [f"mcp__obscura_tools__{t.name}" for t in self._tools]
        if isinstance(choice, ToolChoice):
            if choice.mode == "auto":
                return {}
            if choice.mode == "none":
                return {"disallowed_tools": tool_names} if tool_names else {}
            if choice.mode == "required":
                return {"allowed_tools": tool_names} if tool_names else {}
            if choice.mode == "function" and choice.function_name:
                return {
                    "allowed_tools": [f"mcp__obscura_tools__{choice.function_name}"]
                }
            return {}

        if isinstance(choice, str):
            if choice == "none":
                return {"disallowed_tools": tool_names} if tool_names else {}
            if choice == "required":
                return {"allowed_tools": tool_names} if tool_names else {}
            return {}

        if isinstance(choice, dict):
            return cast(dict[str, Any], choice)
        return {}

    def _sanitize_system_prompt(self, prompt: str) -> str:
        """Strip Claude identity claims from system prompt.
        
        Removes phrases that inject Claude-specific identity to allow
        Obscura agents to run without claiming to be Claude/Anthropic.
        """
        # Patterns to remove
        patterns = [
            r"You are Claude[,\.]?",
            r"I am Claude[,\.]?",
            r"an? AI assistant (made |created |built )?by Anthropic",
            r"You are an? (helpful )?AI assistant",
            r"I am an? (helpful )?AI assistant",
            r"assistant (made |created |built )?by Anthropic",
            r"with access to specialized skills\.?\s*",
        ]
        
        sanitized = prompt
        for pattern in patterns:
            sanitized = re.sub(pattern, "", sanitized, flags=re.IGNORECASE)
        
        # Clean up extra whitespace
        sanitized = re.sub(r'\s+', ' ', sanitized).strip()
        
        return sanitized


    def _build_options(self, **overrides: Any) -> Any:
        """Build ClaudeAgentOptions for the SDK."""
        from claude_agent_sdk import ClaudeAgentOptions

        opts: dict[str, Any] = {}

        if self._model:
            opts["model"] = self._model

        # Build system prompt with dynamic tool listing
        prompt = self._system_prompt or ""
        if self._tools:
            tool_section = self._build_tool_listing()
            prompt = f"{prompt}\n\n{tool_section}" if prompt else tool_section
        if prompt:
            opts["system_prompt"] = self._sanitize_system_prompt(prompt)
        if self._permission_mode:
            opts["permission_mode"] = self._permission_mode
        if self._cwd:
            opts["cwd"] = self._cwd

        # Tools → MCP server
        if self._tools:
            opts["mcp_servers"] = self._build_mcp_tools()
        if self._mcp_servers:
            # Merge external MCP servers, translating Obscura format to
            # Claude SDK format (McpStdioServerConfig / McpSSEServerConfig).
            existing: dict[str, Any] = opts.get("mcp_servers", {})
            for server in self._mcp_servers:
                name: str = server.get("name", f"mcp_{len(existing)}")
                transport = server.get("transport", "stdio")
                if transport == "stdio":
                    entry: dict[str, Any] = {"command": server["command"]}
                    if server.get("args"):
                        entry["args"] = server["args"]
                    if server.get("env"):
                        entry["env"] = server["env"]
                else:
                    entry = {"type": transport, "url": server["url"]}
                    if server.get("env"):
                        entry["env"] = server["env"]
                existing[name] = entry
            opts["mcp_servers"] = existing

        # Hooks
        hooks = self._build_hooks_config()
        if hooks:
            opts["hooks"] = hooks

        # Apply tool policy to filter tools
        if self._tools and self._tool_policy:
            filtered = self._tool_policy.filter_tools(self._tools)
            if len(filtered) < len(self._tools):
                # Only allow filtered tool names via Claude's allowed_tools
                allowed = [f"mcp__obscura_tools__{t.name}" for t in filtered]
                opts["allowed_tools"] = allowed

        opts.update(overrides)
        return ClaudeAgentOptions(**opts)

    def _build_tool_listing(self) -> str:
        """Build a human-readable tool listing for the system prompt."""
        lines = ["## Available Tools", ""]
        lines.append("You have the following tools. Use these EXACT names when calling tools:")
        lines.append("")
        for spec in self._tools:
            desc = (spec.description or "").split("\n")[0][:120]
            lines.append(f"- `{spec.name}`: {desc}")
        lines.append("")
        lines.append("Do NOT invent tool names. If none of these tools fit, tell the user.")
        return "\n".join(lines)

    def _build_mcp_tools(self) -> dict[str, Any]:
        """Convert registered ToolSpecs to a Claude in-process MCP server."""
        from claude_agent_sdk import tool as claude_tool
        from claude_agent_sdk import create_sdk_mcp_server

        claude_tools: list[Any] = []
        for spec in self._tools:
            # Create a claude @tool-decorated function for each ToolSpec
            decorated = claude_tool(
                spec.name,
                spec.description,
                spec.parameters,
            )(spec.handler)
            claude_tools.append(decorated)

        server = create_sdk_mcp_server(
            name="obscura_tools",
            version="1.0.0",
            tools=claude_tools,
        )
        return {"obscura_tools": server}

    def _build_hooks_config(self) -> dict[str, Any] | None:
        """Translate registered hooks to Claude SDK hook config."""
        hook_map: dict[str, str] = {
            HookPoint.PRE_TOOL_USE.value: "PreToolUse",
            HookPoint.POST_TOOL_USE.value: "PostToolUse",
            HookPoint.USER_PROMPT_SUBMITTED.value: "UserPromptSubmit",
            HookPoint.STOP.value: "Stop",
        }

        result: dict[str, list[Any]] = {}
        for hp, callbacks in self._hooks.items():
            if not callbacks:
                continue
            claude_key = hook_map.get(hp.value)
            if not claude_key:
                continue

            # Wrap our callbacks in Claude's HookMatcher format
            try:
                from claude_agent_sdk import HookMatcher

                matchers = [HookMatcher(hooks=callbacks)]
                result[claude_key] = matchers
            except ImportError:
                # Fallback: pass raw callbacks
                result[claude_key] = callbacks

        return result or None

    def _to_message(self, raw_messages: list[Any]) -> Message:
        """Convert Claude response messages to a normalized Message."""
        blocks: list[ContentBlock] = []

        for msg in raw_messages:
            type_name = type(msg).__name__

            if type_name == "AssistantMessage" and hasattr(msg, "content"):
                for block in msg.content:
                    block_type = type(block).__name__

                    if block_type == "TextBlock" and hasattr(block, "text"):
                        blocks.append(ContentBlock(kind="text", text=block.text))
                    elif block_type == "ThinkingBlock" and hasattr(block, "thinking"):
                        blocks.append(
                            ContentBlock(kind="thinking", text=block.thinking)
                        )
                    elif block_type == "ToolUseBlock":
                        blocks.append(
                            ContentBlock(
                                kind="tool_use",
                                tool_name=getattr(block, "name", ""),
                                tool_input=getattr(block, "input", {}),
                                tool_use_id=getattr(block, "id", ""),
                            )
                        )
                    elif block_type == "ToolResultBlock":
                        content = getattr(block, "content", "")
                        if not isinstance(content, str):
                            content = str(content)
                        blocks.append(
                            ContentBlock(
                                kind="tool_result",
                                text=content,
                                tool_use_id=getattr(block, "tool_use_id", ""),
                                is_error=getattr(block, "is_error", False),
                            )
                        )

            elif type_name == "ResultMessage":
                # ResultMessage is metadata, not content — skip
                continue

        if not blocks:
            blocks = [ContentBlock(kind="text", text="")]

        # Use last raw message for the raw field
        raw = raw_messages[-1] if raw_messages else None

        return Message(
            role=Role.ASSISTANT,
            content=blocks,
            raw=raw,
            backend=Backend.CLAUDE,
        )


# ---------------------------------------------------------------------------
# Lazy telemetry helpers
# ---------------------------------------------------------------------------

from obscura.telemetry.traces import NoOpTracer


def _get_backend_tracer() -> Any:
    try:
        from obscura.telemetry.traces import get_tracer

        return get_tracer("obscura.claude_backend")
    except Exception:
        return NoOpTracer()


def _set_span_attr(span: Any, key: str, value: Any) -> None:
    try:
        if hasattr(span, "set_attribute"):
            span.set_attribute(key, value)
    except Exception:
        pass
