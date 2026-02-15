"""
sdk.claude_backend — BackendProtocol implementation for Claude Agent SDK.

Wraps ``claude-agent-sdk`` (``ClaudeSDKClient``, ``query()``) behind the
unified interface. Claude's async-iterator model maps naturally to our
``stream()`` method; ``send()`` simply collects the iterator.
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Callable

from sdk._auth import AuthConfig
from sdk._sessions import SessionStore
from sdk._stream import ClaudeIteratorAdapter
from sdk._tools import ToolRegistry
from sdk._types import (
    AgentEvent,
    Backend,
    ChunkKind,
    ContentBlock,
    HookPoint,
    Message,
    Role,
    SessionRef,
    StreamChunk,
    ToolSpec,
)


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
    ) -> None:
        self._auth = auth
        self._model = model or "claude-sonnet-4-5-20250929"
        self._system_prompt = system_prompt
        self._mcp_servers = mcp_servers or []
        self._permission_mode = permission_mode
        self._cwd = cwd

        # SDK objects (set on start())
        self._client: Any = None
        self._last_session_id: str | None = None

        # Tool and hook registries
        self._tools: list[ToolSpec] = []
        self._tool_registry = ToolRegistry()
        self._hooks: dict[HookPoint, list[Callable[..., Any]]] = {hp: [] for hp in HookPoint}

        # Session tracking
        self._session_store = SessionStore()

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
            await self._client.query(prompt)
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
        with tracer.start_as_current_span("claude.stream") as span:
            _set_span_attr(span, "backend", "claude")

            await self._client.query(prompt)
            source = self._client.receive_response()
            adapter = ClaudeIteratorAdapter(source)

            async for chunk in adapter:
                # Track session ID from done events
                if chunk.kind == ChunkKind.DONE and chunk.raw is not None:
                    if hasattr(chunk.raw, "session_id"):
                        self._last_session_id = chunk.raw.session_id
                yield chunk

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
        from sdk.agent_loop import AgentLoop

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

    def _build_options(self, **overrides: Any) -> Any:
        """Build ClaudeAgentOptions for the SDK."""
        from claude_agent_sdk import ClaudeAgentOptions
        opts: dict[str, Any] = {}

        if self._model:
            opts["model"] = self._model
        if self._system_prompt:
            opts["system_prompt"] = self._system_prompt
        if self._permission_mode:
            opts["permission_mode"] = self._permission_mode
        if self._cwd:
            opts["cwd"] = self._cwd

        # Tools → MCP server
        if self._tools:
            opts["mcp_servers"] = self._build_mcp_tools()
        if self._mcp_servers:
            # Merge external MCP servers
            existing: dict[str, Any] = opts.get("mcp_servers", {})
            for server in self._mcp_servers:
                name: str = server.get("name", f"mcp_{len(existing)}")
                existing[name] = server
            opts["mcp_servers"] = existing

        # Hooks
        hooks = self._build_hooks_config()
        if hooks:
            opts["hooks"] = hooks

        # Allowed tools: expose custom tools by MCP name
        if self._tools:
            allowed = [f"mcp__obscura_tools__{t.name}" for t in self._tools]
            opts["allowed_tools"] = allowed

        opts.update(overrides)
        return ClaudeAgentOptions(**opts)

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
                        blocks.append(ContentBlock(kind="thinking", text=block.thinking))
                    elif block_type == "ToolUseBlock":
                        blocks.append(ContentBlock(
                            kind="tool_use",
                            tool_name=getattr(block, "name", ""),
                            tool_input=getattr(block, "input", {}),
                            tool_use_id=getattr(block, "id", ""),
                        ))
                    elif block_type == "ToolResultBlock":
                        content = getattr(block, "content", "")
                        if not isinstance(content, str):
                            content = str(content)
                        blocks.append(ContentBlock(
                            kind="tool_result",
                            text=content,
                            tool_use_id=getattr(block, "tool_use_id", ""),
                            is_error=getattr(block, "is_error", False),
                        ))

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

from sdk.telemetry.traces import NoOpTracer


def _get_backend_tracer() -> Any:
    try:
        from sdk.telemetry.traces import get_tracer
        return get_tracer("obscura.claude_backend")
    except Exception:
        return NoOpTracer()


def _set_span_attr(span: Any, key: str, value: Any) -> None:
    try:
        if hasattr(span, "set_attribute"):
            span.set_attribute(key, value)
    except Exception:
        pass
