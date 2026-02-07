"""
sdk.copilot_backend — BackendProtocol implementation for GitHub Copilot SDK.

Wraps ``github-copilot-sdk`` (``CopilotClient``, ``CopilotSession``) behind
the unified interface. Copilot's event-push model is bridged to async iterators
via ``EventToIteratorBridge``.
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator, Callable

from sdk._auth import AuthConfig
from sdk._sessions import SessionStore
from sdk._stream import EventToIteratorBridge
from sdk._types import (
    Backend,
    ChunkKind,
    ContentBlock,
    HookContext,
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
    ) -> None:
        self._auth = auth
        self._model = model
        self._system_prompt = system_prompt
        self._mcp_servers = mcp_servers or []
        self._streaming = streaming

        # SDK objects (set on start())
        self._client: Any = None
        self._session: Any = None

        # Tool and hook registries
        self._tools: list[ToolSpec] = []
        self._hooks: dict[HookPoint, list[Callable]] = {hp: [] for hp in HookPoint}

        # Session tracking
        self._session_store = SessionStore()

    # -- Lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        """Initialize the Copilot client and create a default session."""
        from copilot import CopilotClient  # type: ignore[import-untyped]

        client_opts: dict[str, Any] = {}
        if self._auth.github_token:
            client_opts["github_token"] = self._auth.github_token
        if self._auth.byok_provider:
            client_opts["provider"] = self._auth.byok_provider

        self._client = CopilotClient(client_opts if client_opts else None)
        await self._client.start()

        # Create default session
        session_config = self._build_session_config()
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
        response = await self._session.send_and_wait(prompt, kwargs.get("options"))
        return self._to_message(response)

    async def stream(self, prompt: str, **kwargs: Any) -> AsyncIterator[StreamChunk]:
        """Send a prompt and yield streaming chunks."""
        self._ensure_session()
        bridge = EventToIteratorBridge()

        # Register event handlers
        unsub_fns: list[Callable] = []

        def _on_delta(event: Any) -> None:
            bridge.on_text_delta(event)

        def _on_thinking(event: Any) -> None:
            bridge.on_thinking_delta(event)

        def _on_tool_start(event: Any) -> None:
            bridge.on_tool_start(event)

        def _on_idle(event: Any = None) -> None:
            bridge.finish(event)

        def _on_error(event: Any) -> None:
            bridge.error(event)

        # Subscribe to session events
        unsub_fns.append(self._session.on(_make_handler("assistant.message_delta", _on_delta)))
        unsub_fns.append(self._session.on(_make_handler("assistant.reasoning_delta", _on_thinking)))
        unsub_fns.append(self._session.on(_make_handler("tool_execution_start", _on_tool_start)))
        unsub_fns.append(self._session.on(_make_handler("session.idle", _on_idle)))
        unsub_fns.append(self._session.on(_make_handler("session.error", _on_error)))

        # Send the message (non-blocking)
        await self._session.send(prompt, kwargs.get("options"))

        # Yield chunks from the bridge
        try:
            async for chunk in bridge:
                yield chunk
        finally:
            # Unsubscribe all handlers
            for unsub in unsub_fns:
                if callable(unsub):
                    unsub()

    # -- Sessions ------------------------------------------------------------

    async def create_session(self, **kwargs: Any) -> SessionRef:
        """Create a new named session."""
        self._ensure_client()
        config = self._build_session_config(**kwargs)
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

    # -- Tools ---------------------------------------------------------------

    def register_tool(self, spec: ToolSpec) -> None:
        """Register a tool for use in sessions."""
        self._tools.append(spec)

    # -- Hooks ---------------------------------------------------------------

    def register_hook(self, hook: HookPoint, callback: Callable) -> None:
        """Register a lifecycle hook callback."""
        self._hooks[hook].append(callback)

    # -- Internals -----------------------------------------------------------

    def _ensure_client(self) -> None:
        if self._client is None:
            raise RuntimeError("CopilotBackend not started. Call start() first.")

    def _ensure_session(self) -> None:
        self._ensure_client()
        if self._session is None:
            raise RuntimeError("No active session. Call start() or create_session() first.")

    def _build_session_config(self, **overrides: Any) -> dict[str, Any]:
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
            config["tools"] = self._tools  # Backend translates ToolSpecs

        # Apply hook mappings
        hooks = self._build_hooks_config()
        if hooks:
            config["hooks"] = hooks

        config.update(overrides)
        return config

    def _build_hooks_config(self) -> dict[str, Any] | None:
        """Translate registered hooks to Copilot SDK hook config."""
        hook_map: dict[str, str] = {
            HookPoint.PRE_TOOL_USE.value: "on_pre_tool_use",
            HookPoint.POST_TOOL_USE.value: "on_post_tool_use",
            HookPoint.USER_PROMPT_SUBMITTED.value: "on_user_prompt_submitted",
            HookPoint.STOP.value: "on_stop",
        }

        result: dict[str, Any] = {}
        for hp, callbacks in self._hooks.items():
            if callbacks:
                copilot_key = hook_map.get(hp.value)
                if copilot_key and len(callbacks) == 1:
                    result[copilot_key] = callbacks[0]
                elif copilot_key:
                    # Chain multiple callbacks
                    async def _chained(*args: Any, cbs: list[Callable] = callbacks, **kw: Any) -> Any:
                        last_result = None
                        for cb in cbs:
                            last_result = await cb(*args, **kw) if asyncio.iscoroutinefunction(cb) else cb(*args, **kw)
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

def _make_handler(event_type: str, callback: Callable) -> Callable:
    """Create a Copilot event handler that filters by event type.

    Copilot's session.on() may pass all events to a single callback,
    or accept type-specific subscriptions. This wrapper handles both cases.
    """
    def handler(event: Any) -> None:
        # If the event has a type field, filter on it
        if hasattr(event, "type"):
            if event.type == event_type or getattr(event.type, "value", None) == event_type:
                callback(event)
                return
        # If no type filtering possible, just call through
        callback(event)

    # Tag the handler so the SDK can identify it
    handler._event_type = event_type  # type: ignore[attr-defined]
    return handler
