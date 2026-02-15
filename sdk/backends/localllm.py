"""
sdk.backends.localllm — BackendProtocol implementation for local LLM servers.

Connects to OpenAI-compatible local servers (LM Studio, Ollama, llama.cpp,
vLLM, etc.) via the ``openai`` Python SDK in full proxy mode — all traffic
stays on localhost with no API key required.

Full proxy mode: ObscuraClient → openai SDK → local HTTP server → local model.
"""

from __future__ import annotations

import inspect
from typing import Any, AsyncIterator, Callable

from sdk.internal.auth import AuthConfig
from sdk.internal.sessions import SessionStore
from sdk.internal.tools import ToolRegistry
from sdk.internal.types import (
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
from sdk.backends.models import (
    ChatMessage,
    CompletionParams,
    MCPServerConfig,
    ModelInfo,
    ToolCallDefinition,
)


class LocalLLMBackend:
    """BackendProtocol implementation for local LLM servers.

    Uses the ``openai`` Python SDK pointed at a local endpoint. Works with
    any OpenAI-compatible server: LM Studio, Ollama, llama.cpp server, vLLM,
    text-generation-inference, LocalAI, etc.

    Full proxy mode means Obscura acts as a transparent pass-through — the
    openai SDK handles HTTP, SSE parsing, and retries while the local server
    runs the model.
    """

    def __init__(
        self,
        auth: AuthConfig,
        *,
        model: str | None = None,
        system_prompt: str = "",
        mcp_servers: list[dict[str, Any]] | None = None,
    ) -> None:
        self._base_url = auth.localllm_base_url or "http://localhost:1234/v1"
        self._model = model  # None = let server pick default
        self._system_prompt = system_prompt
        self._mcp_servers: list[MCPServerConfig] = [
            MCPServerConfig.from_dict(s) for s in (mcp_servers or [])
        ]

        # SDK client (set on start())
        self._client: Any = None

        # Tool and hook registries
        self._tools: list[ToolSpec] = []
        self._tool_registry = ToolRegistry()
        self._hooks: dict[HookPoint, list[Callable[..., Any]]] = {hp: [] for hp in HookPoint}

        # Session tracking (conversation history per session)
        self._session_store = SessionStore()
        self._conversations: dict[str, list[ChatMessage]] = {}
        self._active_session: str | None = None

    # -- Lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        """Initialize the OpenAI SDK client pointed at the local server."""
        from openai import AsyncOpenAI

        self._client = AsyncOpenAI(
            base_url=self._base_url,
            api_key="not-needed",  # local servers don't require a key
        )

        # Discover default model if none specified
        if self._model is None:
            self._model = await self._discover_model()

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
        with tracer.start_as_current_span("localllm.send") as span:
            _set_span_attr(span, "backend", "localllm")
            _set_span_attr(span, "model", self._model)
            _set_span_attr(span, "base_url", self._base_url)

            await self._run_hooks(HookContext(hook=HookPoint.USER_PROMPT_SUBMITTED, prompt=prompt))

            messages = self._build_messages(prompt)
            create_kwargs = self._build_create_kwargs(kwargs)

            response = await self._client.chat.completions.create(
                model=self._model or "default",
                messages=messages,
                **create_kwargs,
            )

            msg = self._to_message(response)

            # Persist conversation history
            if self._active_session and self._active_session in self._conversations:
                self._conversations[self._active_session].append(ChatMessage(role="user", content=prompt))
                self._conversations[self._active_session].append(ChatMessage(role="assistant", content=msg.text))

            await self._run_hooks(HookContext(hook=HookPoint.STOP))

            return msg

    async def stream(self, prompt: str, **kwargs: Any) -> AsyncIterator[StreamChunk]:
        """Send a prompt and yield streaming chunks."""
        self._ensure_client()
        tracer = _get_backend_tracer()
        with tracer.start_as_current_span("localllm.stream") as span:
            _set_span_attr(span, "backend", "localllm")
            _set_span_attr(span, "model", self._model)

            await self._run_hooks(HookContext(hook=HookPoint.USER_PROMPT_SUBMITTED, prompt=prompt))

            messages = self._build_messages(prompt)
            create_kwargs = self._build_create_kwargs(kwargs)

            response = await self._client.chat.completions.create(
                model=self._model or "default",
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
                self._conversations[self._active_session].append(ChatMessage(role="user", content=prompt))
                self._conversations[self._active_session].append(ChatMessage(role="assistant", content=accumulated_text))

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
            backend=Backend.LOCALLLM,
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
        return self._session_store.list_all(Backend.LOCALLLM)

    async def delete_session(self, ref: SessionRef) -> None:
        """Delete a session."""
        self._conversations.pop(ref.session_id, None)
        self._session_store.remove(ref.session_id)

    # -- Tools ---------------------------------------------------------------

    def register_tool(self, spec: ToolSpec) -> None:
        """Register a tool."""
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
        from sdk.agent.agent_loop import AgentLoop

        loop = AgentLoop(
            self,
            self._tool_registry,
            max_turns=max_turns,
            on_confirm=on_confirm,
        )
        return loop.run(prompt, **kwargs)

    # -- Local LLM-specific methods (escape hatch) ---------------------------

    async def list_models(self) -> list[dict[str, Any]]:
        """List models available on the local server."""
        self._ensure_client()
        models = await self._client.models.list()
        return [ModelInfo.from_openai(m).to_dict() for m in models.data]

    async def health_check(self) -> dict[str, Any]:
        """Check if the local server is reachable."""
        try:
            models = await self._client.models.list()
            return {
                "status": "healthy",
                "base_url": self._base_url,
                "models_available": len(models.data),
            }
        except Exception as e:
            return {
                "status": "unhealthy",
                "base_url": self._base_url,
                "error": str(e),
            }

    # -- Internals -----------------------------------------------------------

    def _ensure_client(self) -> None:
        if self._client is None:
            raise RuntimeError("LocalLLMBackend not started. Call start() first.")

    def _build_messages(self, prompt: str) -> list[dict[str, str]]:
        """Build the messages list for the chat completions API."""
        messages: list[dict[str, str]] = []
        if self._system_prompt:
            messages.append({"role": "system", "content": self._system_prompt})

        # Append conversation history if in a session
        if self._active_session and self._active_session in self._conversations:
            messages.extend([msg.to_dict() for msg in self._conversations[self._active_session]])

        messages.append({"role": "user", "content": prompt})
        return messages

    async def _discover_model(self) -> str | None:
        """Try to discover the first available model on the server."""
        try:
            models = await self._client.models.list()
            if models.data:
                return models.data[0].id
        except Exception:
            pass
        return None

    def _build_create_kwargs(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        """Build kwargs for chat.completions.create, including tool defs."""
        params = CompletionParams.from_kwargs(kwargs)
        result: dict[str, Any] = params.to_dict()

        if self._tools:
            result["tools"] = [
                ToolCallDefinition(t.name, t.description, t.parameters).to_openai_function()
                for t in self._tools
            ]

        return result

    def _to_message(self, response: Any) -> Message:
        """Convert an OpenAI-compatible response to a normalized Message."""
        choice = response.choices[0]
        msg = choice.message
        blocks: list[ContentBlock] = []

        if msg.content:
            blocks.append(ContentBlock(kind="text", text=msg.content))

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
            backend=Backend.LOCALLLM,
        )

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


# ---------------------------------------------------------------------------
# Lazy telemetry helpers
# ---------------------------------------------------------------------------

from sdk.telemetry.traces import NoOpTracer


def _get_backend_tracer() -> Any:
    try:
        from sdk.telemetry.traces import get_tracer
        return get_tracer("obscura.localllm_backend")
    except Exception:
        return NoOpTracer()


def _set_span_attr(span: Any, key: str, value: Any) -> None:
    try:
        if hasattr(span, "set_attribute"):
            span.set_attribute(key, value)
    except Exception:
        pass
