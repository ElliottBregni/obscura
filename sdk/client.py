"""
sdk.client — ObscuraClient: unified entry point for Copilot and Claude.

Dispatches to the appropriate backend based on the ``backend`` parameter.
Integrates with ``copilot_models`` for model alias resolution and safety
guards.
"""

from __future__ import annotations

import sys
from typing import Any, AsyncIterator, Callable

from sdk._auth import AuthConfig, resolve_auth
from sdk._sessions import SessionStore
from sdk._tools import ToolRegistry
from sdk._types import (
    Backend,
    BackendProtocol,
    HookPoint,
    Message,
    SessionRef,
    StreamChunk,
    ToolSpec,
)


# ---------------------------------------------------------------------------
# Unified client
# ---------------------------------------------------------------------------

class ObscuraClient:
    """Unified SDK client that dispatches to Copilot or Claude.

    Usage::

        async with ObscuraClient("copilot", model_alias="copilot_automation_safe") as client:
            response = await client.send("explain this code")
            print(response.text)

        async with ObscuraClient("claude", model="claude-sonnet-4-5-20250929") as client:
            async for chunk in client.stream("count to 5"):
                print(chunk.text, end="", flush=True)
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
    ) -> None:
        if isinstance(backend, str):
            backend = Backend(backend)
        self._backend_type = backend

        # Resolve model via copilot_models aliases
        resolved_model = self._resolve_model(
            backend, model, model_alias, automation_safe,
        )

        # Resolve auth
        resolved_auth = resolve_auth(backend, auth)

        # Build tool registry
        self._tool_registry = ToolRegistry()
        for t in (tools or []):
            self._tool_registry.register(t)

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

        # Register tools with backend
        for t in self._tool_registry.all():
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
        return await self._backend.send(prompt, **kwargs)

    async def stream(self, prompt: str, **kwargs: Any) -> AsyncIterator[StreamChunk]:
        """Send prompt, yield streaming chunks."""
        async for chunk in self._backend.stream(prompt, **kwargs):
            yield chunk

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

    # -- Tools ---------------------------------------------------------------

    def register_tool(self, spec: ToolSpec) -> None:
        """Register a tool with the active backend."""
        self._tool_registry.register(spec)
        self._backend.register_tool(spec)

    # -- Hooks ---------------------------------------------------------------

    def on(self, hook: HookPoint, callback: Callable) -> None:
        """Register a hook callback."""
        self._backend.register_hook(hook, callback)

    # -- Backend access (escape hatch) --------------------------------------

    @property
    def backend_impl(self) -> Any:
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
            try:
                from copilot_models import require_automation_safe, resolve

                if automation_safe:
                    config = require_automation_safe(model_alias)
                else:
                    config = resolve(model_alias)
                return config.model_id
            except ImportError:
                print(
                    "[sdk] Warning: copilot_models not found, using alias as model ID.",
                    file=sys.stderr,
                )
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
    ) -> Any:
        """Instantiate the appropriate backend."""
        if backend == Backend.COPILOT:
            from sdk.copilot_backend import CopilotBackend

            return CopilotBackend(
                auth=auth,
                model=model,
                system_prompt=system_prompt,
                mcp_servers=mcp_servers,
                streaming=streaming,
            )

        if backend == Backend.CLAUDE:
            from sdk.claude_backend import ClaudeBackend

            return ClaudeBackend(
                auth=auth,
                model=model,
                system_prompt=system_prompt,
                mcp_servers=mcp_servers,
                permission_mode=permission_mode,
                cwd=cwd,
            )

        raise ValueError(f"Unknown backend: {backend}")
