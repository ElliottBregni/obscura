"""
sdk.backends.openai_compat — BackendProtocol implementation for the OpenAI SDK.

Full proxy mode: ObscuraClient → openai Python SDK → OpenAI API (or any
OpenAI-compatible provider: OpenRouter, Together, Groq, Fireworks, etc.).

The openai SDK handles all HTTP transport, auth headers, retries, and SSE
streaming. Obscura normalizes the responses into its unified Message /
StreamChunk types.
"""

from __future__ import annotations

import inspect
from typing import Any, AsyncIterator, Callable

from sdk._auth import AuthConfig
from sdk._sessions import SessionStore
from sdk._tools import ToolRegistry
from sdk._types import (
    AgentEvent,
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


class OpenAIBackend:
    """BackendProtocol implementation wrapping the official ``openai`` SDK.

    Full proxy mode means the openai SDK owns the entire HTTP lifecycle —
    Obscura adds agent orchestration, tool dispatch, hooks, memory, and
    telemetry on top.

    Supports any OpenAI-compatible provider by setting ``base_url``:
    - OpenAI (default): ``https://api.openai.com/v1``
    - OpenRouter: ``https://openrouter.ai/api/v1``
    - Together: ``https://api.together.xyz/v1``
    - Groq: ``https://api.groq.com/openai/v1``
    - Fireworks: ``https://api.fireworks.ai/inference/v1``
    """

    def __init__(
        self,
        auth: AuthConfig,
        *,
        model: str | None = None,
        system_prompt: str = "",
        mcp_servers: list[dict[str, Any]] | None = None,
    ) -> None:
        self._api_key = auth.openai_api_key or ""
        self._base_url = auth.openai_base_url  # None = OpenAI default
        self._model = model or "gpt-4o"
        self._system_prompt = system_prompt
        self._mcp_servers = mcp_servers or []

        # SDK client (set on start())
        self._client: Any = None

        # Tool and hook registries
        self._tools: list[ToolSpec] = []
        self._tool_registry = ToolRegistry()
        self._hooks: dict[HookPoint, list[Callable[..., Any]]] = {hp: [] for hp in HookPoint}

        # Session tracking
        self._session_store = SessionStore()
        self._conversations: dict[str, list[dict[str, Any]]] = {}
        self._active_session: str | None = None

    # -- Lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        """Initialize the OpenAI SDK client."""
        from openai import AsyncOpenAI

        kwargs: dict[str, Any] = {"api_key": self._api_key}
        if self._base_url:
            kwargs["base_url"] = self._base_url

        self._client = AsyncOpenAI(**kwargs)

    async def stop(self) -> None:
        """Close the client."""
        if self._client is not None:
            await self._client.close()
            self._client = None

    # -- Send / Stream -------------------------------------------------------

    async def send(self, prompt: str, **kwargs: Any) -> Message:
        """Send a prompt and wait for the full response."""
        self._ensure_client()
        tracer = _get_backend_tracer()
        with tracer.start_as_current_span("openai.send") as span:
            _set_span_attr(span, "backend", "openai")
            _set_span_attr(span, "model", self._model)

            await self._run_hooks(HookContext(hook=HookPoint.USER_PROMPT_SUBMITTED, prompt=prompt))

            messages = self._build_messages(prompt)
            create_kwargs = self._build_create_kwargs(kwargs)

            response = await self._client.chat.completions.create(
                model=self._model,
                messages=messages,
                **create_kwargs,
            )

            msg = self._to_message(response)

            # Persist conversation history
            if self._active_session and self._active_session in self._conversations:
                self._conversations[self._active_session].append({"role": "user", "content": prompt})
                self._conversations[self._active_session].append({"role": "assistant", "content": msg.text})

            await self._run_hooks(HookContext(hook=HookPoint.STOP))

            return msg

    async def stream(self, prompt: str, **kwargs: Any) -> AsyncIterator[StreamChunk]:
        """Send a prompt and yield streaming chunks."""
        self._ensure_client()
        tracer = _get_backend_tracer()
        with tracer.start_as_current_span("openai.stream") as span:
            _set_span_attr(span, "backend", "openai")
            _set_span_attr(span, "model", self._model)

            await self._run_hooks(HookContext(hook=HookPoint.USER_PROMPT_SUBMITTED, prompt=prompt))

            messages = self._build_messages(prompt)
            create_kwargs = self._build_create_kwargs(kwargs)

            response = await self._client.chat.completions.create(
                model=self._model,
                messages=messages,
                stream=True,
                **create_kwargs,
            )

            accumulated_text = ""
            async for chunk in response:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta

                # Tool call deltas
                if delta.tool_calls:
                    for tc in delta.tool_calls:
                        if tc.function and tc.function.name:
                            yield StreamChunk(
                                kind=ChunkKind.TOOL_USE_START,
                                tool_name=tc.function.name,
                                raw=chunk,
                            )
                        if tc.function and tc.function.arguments:
                            yield StreamChunk(
                                kind=ChunkKind.TOOL_USE_DELTA,
                                tool_input_delta=tc.function.arguments,
                                raw=chunk,
                            )

                # Text content
                if delta.content:
                    accumulated_text += delta.content
                    yield StreamChunk(
                        kind=ChunkKind.TEXT_DELTA,
                        text=delta.content,
                        raw=chunk,
                    )

            # Persist conversation history
            if self._active_session and self._active_session in self._conversations:
                self._conversations[self._active_session].append({"role": "user", "content": prompt})
                self._conversations[self._active_session].append({"role": "assistant", "content": accumulated_text})

            await self._run_hooks(HookContext(hook=HookPoint.STOP))

            yield StreamChunk(kind=ChunkKind.DONE, raw=None)

    # -- Sessions ------------------------------------------------------------

    async def create_session(self, **kwargs: Any) -> SessionRef:
        """Create a new conversation session."""
        import uuid
        session_id = str(uuid.uuid4())
        self._conversations[session_id] = []
        ref = SessionRef(
            session_id=session_id,
            backend=Backend.OPENAI,
        )
        self._session_store.add(ref)
        self._active_session = session_id
        return ref

    async def resume_session(self, ref: SessionRef) -> None:
        """Resume a conversation session."""
        if ref.session_id not in self._conversations:
            raise RuntimeError(f"Session {ref.session_id} not found")
        self._active_session = ref.session_id

    async def list_sessions(self) -> list[SessionRef]:
        """List tracked sessions."""
        return self._session_store.list_all(Backend.OPENAI)

    async def delete_session(self, ref: SessionRef) -> None:
        """Delete a session."""
        self._conversations.pop(ref.session_id, None)
        self._session_store.remove(ref.session_id)

    # -- Tools ---------------------------------------------------------------

    def register_tool(self, spec: ToolSpec) -> None:
        """Register a tool for function calling."""
        self._tools.append(spec)
        self._tool_registry.register(spec)

    def get_tool_registry(self) -> ToolRegistry:
        """Return the tool registry."""
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
        """Run an iterative agent loop with tool execution."""
        from sdk.agent_loop import AgentLoop

        loop = AgentLoop(
            self,
            self._tool_registry,
            max_turns=max_turns,
            on_confirm=on_confirm,
        )
        return loop.run(prompt, **kwargs)

    async def _run_hooks(self, context: HookContext) -> None:
        """Run all registered hooks for a given hook point."""
        callbacks = self._hooks.get(context.hook, [])
        for callback in callbacks:
            try:
                if inspect.iscoroutinefunction(callback):
                    await callback(context)
                else:
                    callback(context)
            except Exception:
                pass

    # -- OpenAI-specific methods (escape hatch) ------------------------------

    async def list_models(self) -> list[dict[str, Any]]:
        """List models available from the provider."""
        self._ensure_client()
        models = await self._client.models.list()
        return [{"id": m.id, "object": m.object} for m in models.data]

    # -- Internals -----------------------------------------------------------

    def _ensure_client(self) -> None:
        if self._client is None:
            raise RuntimeError("OpenAIBackend not started. Call start() first.")

    def _build_messages(self, prompt: str) -> list[dict[str, str]]:
        """Build the messages list for the chat completions API."""
        messages: list[dict[str, str]] = []
        if self._system_prompt:
            messages.append({"role": "system", "content": self._system_prompt})

        if self._active_session and self._active_session in self._conversations:
            messages.extend(self._conversations[self._active_session])

        messages.append({"role": "user", "content": prompt})
        return messages

    def _build_create_kwargs(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        """Build kwargs for chat.completions.create, including tool defs."""
        result: dict[str, Any] = {}

        # Pass through valid completion params
        valid = {
            "temperature", "top_p", "max_tokens", "stop",
            "frequency_penalty", "presence_penalty", "seed",
            "response_format",
        }
        for k, v in kwargs.items():
            if k in valid:
                result[k] = v

        # Register tools as OpenAI function calling format
        if self._tools:
            result["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in self._tools
            ]

        return result

    def _to_message(self, response: Any) -> Message:
        """Convert an OpenAI response to a normalized Message."""
        choice = response.choices[0]
        msg = choice.message
        blocks: list[ContentBlock] = []

        # Text content
        if msg.content:
            blocks.append(ContentBlock(kind="text", text=msg.content))

        # Tool calls
        if msg.tool_calls:
            import json
            for tc in msg.tool_calls:
                try:
                    tool_input = json.loads(tc.function.arguments)
                except (json.JSONDecodeError, TypeError):
                    tool_input = {"raw": tc.function.arguments}
                blocks.append(ContentBlock(
                    kind="tool_use",
                    tool_name=tc.function.name,
                    tool_input=tool_input,
                    tool_use_id=tc.id,
                ))

        if not blocks:
            blocks = [ContentBlock(kind="text", text="")]

        return Message(
            role=Role.ASSISTANT,
            content=blocks,
            raw=response,
            backend=Backend.OPENAI,
        )


# ---------------------------------------------------------------------------
# Lazy telemetry helpers
# ---------------------------------------------------------------------------

from sdk.telemetry.traces import NoOpTracer


def _get_backend_tracer() -> Any:
    try:
        from sdk.telemetry.traces import get_tracer
        return get_tracer("obscura.openai_backend")
    except Exception:
        return NoOpTracer()


def _set_span_attr(span: Any, key: str, value: Any) -> None:
    try:
        if hasattr(span, "set_attribute"):
            span.set_attribute(key, value)
    except Exception:
        pass
