"""
obscura.backends.openai_compat — BackendProtocol implementation for the OpenAI SDK.

Full proxy mode: ObscuraClient → openai Python SDK → OpenAI API (or any
OpenAI-compatible provider: OpenRouter, Together, Groq, Fireworks, etc.).

The openai SDK handles all HTTP transport, auth headers, retries, and SSE
streaming. Obscura normalizes the responses into its unified Message /
StreamChunk types.
"""

from __future__ import annotations

import inspect
import json
from typing import Any, AsyncIterator, Callable, Mapping, cast

from obscura.core.auth import AuthConfig
from obscura.core.sessions import SessionStore
from obscura.core.tools import ToolRegistry
from obscura.core.types import (
    AgentEvent,
    Backend,
    BackendCapabilities,
    ChunkKind,
    ContentBlock,
    HookContext,
    HookPoint,
    Message,
    NativeHandle,
    Role,
    SessionRef,
    StreamChunk,
    StreamMetadata,
    ToolChoice,
    ToolSpec,
    ExecutionMode,
    ProviderNativeRequest,
    UnifiedRequest,
)
from obscura.providers.models import (
    ChatMessage,
    CompletionParams,
    MCPServerConfig,
    ModelInfo,
    ToolCallDefinition,
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
        backend_type: Backend = Backend.OPENAI,
    ) -> None:
        self._api_key = auth.openai_api_key or ""
        self._base_url = auth.openai_base_url  # None = OpenAI default
        self._model = model or "gpt-4o"
        self._system_prompt = system_prompt
        self._mcp_servers: list[MCPServerConfig] = [
            MCPServerConfig.from_dict(s) for s in (mcp_servers or [])
        ]
        self._backend_type = backend_type

        # SDK client (set on start())
        self._client: Any = None

        # Tool and hook registries
        self._tools: list[ToolSpec] = []
        self._tool_registry = ToolRegistry()
        self._hooks: dict[HookPoint, list[Callable[..., Any]]] = {
            hp: [] for hp in HookPoint
        }

        # Session tracking
        self._session_store = SessionStore()
        self._conversations: dict[str, list[ChatMessage]] = {}
        self._active_session: str | None = None

    # -- Testing/observability accessors ------------------------------------

    @property
    def api_key(self) -> str:
        return self._api_key

    @property
    def base_url(self) -> str | None:
        return self._base_url

    @property
    def model(self) -> str:
        return self._model

    @property
    def system_prompt(self) -> str:
        return self._system_prompt

    @property
    def client(self) -> Any:
        return self._client

    @property
    def tools(self) -> list[ToolSpec]:
        return self._tools

    @property
    def hooks(self) -> dict[HookPoint, list[Callable[..., Any]]]:
        return self._hooks

    @property
    def tool_registry(self) -> ToolRegistry:
        return self._tool_registry

    @property
    def active_session(self) -> str | None:
        return self._active_session

    @property
    def conversations(self) -> dict[str, list[ChatMessage]]:
        return self._conversations

    @property
    def native(self) -> NativeHandle:
        """Raw SDK access for escape-hatch usage."""
        return NativeHandle(client=self._client)

    def capabilities(self) -> BackendCapabilities:
        """Declare what this backend supports."""
        return BackendCapabilities(
            supports_streaming=True,
            supports_tool_calls=True,
            supports_tool_choice=True,
            supports_usage=True,
            supports_native_mode=True,
            native_features=(
                "responses_api",
                "chat_completions",
                "models_list",
                "native_client",
            ),
        )

    def set_client_for_testing(self, client: Any) -> None:
        self._client = client

    def set_active_session_for_testing(self, session_id: str | None) -> None:
        self._active_session = session_id

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
        prompt, structured, api_mode, native_openai = self._resolve_request(
            prompt, kwargs
        )
        tracer = _get_backend_tracer()
        with tracer.start_as_current_span("openai.send") as span:
            _set_span_attr(span, "backend", "openai")
            _set_span_attr(span, "model", self._model)

            await self._run_hooks(
                HookContext(hook=HookPoint.USER_PROMPT_SUBMITTED, prompt=prompt)
            )

            if api_mode == "responses":
                msg = await self._send_via_responses(prompt, kwargs, native_openai)
            else:
                messages = self.build_messages(prompt, structured_messages=structured)
                create_kwargs = self.build_create_kwargs(kwargs)

                response = await self._client.chat.completions.create(
                    model=self._model,
                    messages=messages,
                    **create_kwargs,
                )
                msg = self.to_message(response)

            tool_blocks = [b for b in msg.content if b.kind == "tool_use"]
            for block in tool_blocks:
                await self._run_hooks(
                    HookContext(
                        hook=HookPoint.PRE_TOOL_USE,
                        tool_name=block.tool_name,
                        tool_input=block.tool_input,
                        message=msg,
                    )
                )
                await self._run_hooks(
                    HookContext(
                        hook=HookPoint.POST_TOOL_USE,
                        tool_name=block.tool_name,
                        tool_input=block.tool_input,
                        message=msg,
                    )
                )

            # Persist conversation history (including tool calls)
            if self._active_session and self._active_session in self._conversations:
                self._conversations[self._active_session].append(
                    ChatMessage(role="user", content=prompt)
                )
                # Store tool_calls if present
                if tool_blocks:
                    import json as _json

                    tc_list: list[dict[str, Any]] = [
                        {
                            "id": b.tool_use_id,
                            "type": "function",
                            "function": {
                                "name": b.tool_name,
                                "arguments": _json.dumps(b.tool_input),
                            },
                        }
                        for b in tool_blocks
                    ]
                    self._conversations[self._active_session].append(
                        ChatMessage(
                            role="assistant",
                            content=msg.text,
                            tool_calls=tc_list,
                        )
                    )
                else:
                    self._conversations[self._active_session].append(
                        ChatMessage(role="assistant", content=msg.text)
                    )

            await self._run_hooks(HookContext(hook=HookPoint.STOP))

            return msg

    async def stream(self, prompt: str, **kwargs: Any) -> AsyncIterator[StreamChunk]:
        """Send a prompt and yield streaming chunks."""
        self._ensure_client()
        prompt, structured, api_mode, native_openai = self._resolve_request(
            prompt, kwargs
        )
        tracer = _get_backend_tracer()
        span = tracer.start_span("openai.stream")
        _set_span_attr(span, "backend", "openai")
        _set_span_attr(span, "model", self._model)
        finish_reason = ""
        try:
            await self._run_hooks(
                HookContext(hook=HookPoint.USER_PROMPT_SUBMITTED, prompt=prompt)
            )

            if api_mode == "responses":
                async for chunk in self._stream_via_responses(
                    prompt, kwargs, native_openai
                ):
                    yield chunk
                await self._run_hooks(HookContext(hook=HookPoint.STOP))
                return

            messages = self.build_messages(prompt, structured_messages=structured)
            create_kwargs = self.build_create_kwargs(kwargs)

            response = await self._client.chat.completions.create(
                model=self._model,
                messages=messages,
                stream=True,
                **create_kwargs,
            )

            yield StreamChunk(kind=ChunkKind.MESSAGE_START)

            accumulated_text = ""
            _active_tool_name = ""
            _active_tool_id = ""
            _active_tool_input = ""
            async for chunk in response:
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                delta = choice.delta

                # Track finish_reason from the final chunk
                if choice.finish_reason:
                    finish_reason = choice.finish_reason

                # Tool call deltas
                if delta.tool_calls:
                    for tc in delta.tool_calls:
                        if tc.function and tc.function.name:
                            # Close previous tool if any
                            if _active_tool_name:
                                tool_input = _parse_tool_input(_active_tool_input)
                                yield StreamChunk(
                                    kind=ChunkKind.TOOL_USE_END,
                                    tool_name=_active_tool_name,
                                )
                                await self._run_hooks(
                                    HookContext(
                                        hook=HookPoint.POST_TOOL_USE,
                                        tool_name=_active_tool_name,
                                        tool_input=tool_input,
                                    )
                                )
                                _active_tool_input = ""
                            _active_tool_name = tc.function.name
                            _active_tool_id = tc.id or ""
                            yield StreamChunk(
                                kind=ChunkKind.TOOL_USE_START,
                                tool_name=tc.function.name,
                                tool_use_id=_active_tool_id,
                                raw=chunk,
                                native_event=chunk,
                            )
                            await self._run_hooks(
                                HookContext(
                                    hook=HookPoint.PRE_TOOL_USE,
                                    tool_name=_active_tool_name,
                                    tool_input={},
                                )
                            )
                        if tc.function and tc.function.arguments:
                            _active_tool_input += tc.function.arguments
                            yield StreamChunk(
                                kind=ChunkKind.TOOL_USE_DELTA,
                                tool_input_delta=tc.function.arguments,
                                raw=chunk,
                                native_event=chunk,
                            )

                # Text content
                if delta.content:
                    accumulated_text += delta.content
                    yield StreamChunk(
                        kind=ChunkKind.TEXT_DELTA,
                        text=delta.content,
                        raw=chunk,
                        native_event=chunk,
                    )

            # Close final tool if any
            if _active_tool_name:
                tool_input = _parse_tool_input(_active_tool_input)
                yield StreamChunk(
                    kind=ChunkKind.TOOL_USE_END,
                    tool_name=_active_tool_name,
                )
                await self._run_hooks(
                    HookContext(
                        hook=HookPoint.POST_TOOL_USE,
                        tool_name=_active_tool_name,
                        tool_input=tool_input,
                    )
                )

            # Persist conversation history
            if self._active_session and self._active_session in self._conversations:
                self._conversations[self._active_session].append(
                    ChatMessage(role="user", content=prompt)
                )
                self._conversations[self._active_session].append(
                    ChatMessage(role="assistant", content=accumulated_text)
                )

            await self._run_hooks(HookContext(hook=HookPoint.STOP))
        finally:
            span.end()

            yield StreamChunk(
                kind=ChunkKind.DONE,
                raw=None,
                metadata=StreamMetadata(
                    finish_reason=finish_reason,
                    model_id=self._model,
                ),
            )

    # -- Sessions ------------------------------------------------------------

    async def create_session(self, **kwargs: Any) -> SessionRef:
        """Create a new conversation session."""
        import uuid

        session_id = str(uuid.uuid4())
        self._conversations[session_id] = []
        ref = SessionRef(
            session_id=session_id,
            backend=self._backend_type,
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
        return self._session_store.list_all(self._backend_type)

    async def delete_session(self, ref: SessionRef) -> None:
        """Delete a session."""
        self._conversations.pop(ref.session_id, None)
        self._session_store.remove(ref.session_id)

    async def fork_session(self, ref: SessionRef) -> SessionRef:
        """Fork a session by cloning conversation history into a new session."""
        import copy
        import uuid

        source = self._conversations.get(ref.session_id)
        if source is None:
            raise RuntimeError(f"Session {ref.session_id} not found")

        session_id = str(uuid.uuid4())
        self._conversations[session_id] = copy.deepcopy(source)
        fork_ref = SessionRef(
            session_id=session_id,
            backend=self._backend_type,
            raw={"forked_from": ref.session_id},
        )
        self._session_store.add(fork_ref)
        self._active_session = session_id
        return fork_ref

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
        from obscura.core.agent_loop import AgentLoop

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
        return [ModelInfo.from_openai(m).to_dict() for m in models.data]

    # -- Internals -----------------------------------------------------------

    def _ensure_client(self) -> None:
        if self._client is None:
            raise RuntimeError("OpenAIBackend not started. Call start() first.")

    def _resolve_request(
        self,
        prompt: str,
        kwargs: dict[str, Any],
    ) -> tuple[str, list[Message] | None, str, Mapping[str, Any] | None]:
        """Resolve unified/native mode inputs into an execution plan."""
        structured = cast(list[Message] | None, kwargs.pop("messages", None))
        mode_raw = kwargs.pop("mode", ExecutionMode.UNIFIED.value)
        api_mode = kwargs.pop("api_mode", None)
        native = kwargs.pop("native", None)
        request_obj = kwargs.pop("request", None)

        if isinstance(request_obj, UnifiedRequest):
            if request_obj.prompt:
                prompt = request_obj.prompt
            if request_obj.messages is not None:
                structured = request_obj.messages
            mode_raw = request_obj.mode.value
            if request_obj.native is not None:
                native = request_obj.native

        native_openai = self._extract_native_openai(native)
        if api_mode is None and native_openai is not None:
            raw_api_mode = native_openai.get("api_mode")
            if isinstance(raw_api_mode, str):
                api_mode = raw_api_mode

        mode = mode_raw if isinstance(mode_raw, str) else ExecutionMode.UNIFIED.value
        if api_mode is None and mode == ExecutionMode.NATIVE.value:
            api_mode = "responses"

        if api_mode not in ("chat_completions", "responses"):
            api_mode = "chat_completions"

        return prompt, structured, api_mode, native_openai

    def _extract_native_openai(self, native: Any) -> Mapping[str, Any] | None:
        """Extract openai native payload from dicts or ProviderNativeRequest."""
        if isinstance(native, ProviderNativeRequest):
            if self._backend_type == Backend.MOONSHOT and native.moonshot is not None:
                return native.moonshot
            return native.openai
        if isinstance(native, Mapping):
            if self._backend_type == Backend.MOONSHOT:
                if "moonshot" in native and isinstance(native["moonshot"], Mapping):
                    return cast(Mapping[str, Any], native["moonshot"])
            if "openai" in native and isinstance(native["openai"], Mapping):
                return cast(Mapping[str, Any], native["openai"])
            return cast(Mapping[str, Any], native)
        return None

    async def _send_via_responses(
        self,
        prompt: str,
        kwargs: dict[str, Any],
        native_openai: Mapping[str, Any] | None,
    ) -> Message:
        """Execute a non-streaming request through Responses API."""
        req = self._build_responses_kwargs(prompt, kwargs, native_openai)
        response = await self._client.responses.create(**req)
        text = self._extract_responses_text(response)
        return Message(
            role=Role.ASSISTANT,
            content=[ContentBlock(kind="text", text=text)],
            raw=response,
            backend=self._backend_type,
        )

    async def _stream_via_responses(
        self,
        prompt: str,
        kwargs: dict[str, Any],
        native_openai: Mapping[str, Any] | None,
    ) -> AsyncIterator[StreamChunk]:
        """Execute a streaming request through Responses API."""
        req = self._build_responses_kwargs(prompt, kwargs, native_openai)
        response = await self._client.responses.create(stream=True, **req)

        yield StreamChunk(kind=ChunkKind.MESSAGE_START)
        finish_reason = ""
        async for event in response:
            event_type = self._event_type(event)
            delta = self._extract_response_delta(event)
            if delta:
                yield StreamChunk(
                    kind=ChunkKind.TEXT_DELTA,
                    text=delta,
                    raw=event,
                    native_event=event,
                )
            if event_type in ("response.completed", "response.failed"):
                finish_reason = self._extract_finish_reason(event) or finish_reason

        yield StreamChunk(
            kind=ChunkKind.DONE,
            metadata=StreamMetadata(
                finish_reason=finish_reason,
                model_id=self._model,
            ),
        )

    def _build_responses_kwargs(
        self,
        prompt: str,
        kwargs: dict[str, Any],
        native_openai: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        """Build argument payload for ``client.responses.create``."""
        req: dict[str, Any] = dict(native_openai or {})
        req.pop("api_mode", None)
        req.pop("stream", None)
        req.setdefault("model", self._model)
        req.setdefault("input", prompt)
        if self._system_prompt and "instructions" not in req:
            req["instructions"] = self._system_prompt
        if self._tools and "tools" not in req:
            req["tools"] = [
                ToolCallDefinition(
                    t.name, t.description, t.parameters
                ).to_openai_function()
                for t in self._tools
            ]
        # Allow explicit low-level overrides from call kwargs.
        if "response_create_kwargs" in kwargs:
            extra = kwargs["response_create_kwargs"]
            if isinstance(extra, Mapping):
                extra_map = cast(Mapping[str, Any], extra)
                req.update(dict(extra_map))
        return req

    @staticmethod
    def _extract_responses_text(response: Any) -> str:
        """Extract assistant text from a Responses API object."""
        output_text = getattr(response, "output_text", None)
        if isinstance(output_text, str):
            return output_text

        out = getattr(response, "output", None)
        if isinstance(out, list):
            parts: list[str] = []
            out_list = cast(list[Any], out)
            for item in out_list:
                content = getattr(item, "content", None)
                if isinstance(content, list):
                    content_list = cast(list[Any], content)
                    for c in content_list:
                        txt = getattr(c, "text", None)
                        if isinstance(txt, str):
                            parts.append(txt)
                        elif isinstance(c, Mapping):
                            c_map = cast(Mapping[str, Any], c)
                            mapped = c_map.get("text")
                            if isinstance(mapped, str):
                                parts.append(mapped)
            if parts:
                return "".join(parts)
        return ""

    @staticmethod
    def _event_type(event: Any) -> str:
        if hasattr(event, "type"):
            t = getattr(event, "type")
            if isinstance(t, str):
                return t
        if isinstance(event, Mapping):
            event_map = cast(Mapping[str, Any], event)
            t = event_map.get("type")
            if isinstance(t, str):
                return t
        return ""

    @staticmethod
    def _extract_response_delta(event: Any) -> str:
        """Extract text delta from a Responses stream event."""
        for key in ("delta", "text"):
            val = getattr(event, key, None)
            if isinstance(val, str) and val:
                return val
        if isinstance(event, Mapping):
            event_map = cast(Mapping[str, Any], event)
            for key in ("delta", "text"):
                val = event_map.get(key)
                if isinstance(val, str) and val:
                    return val
        return ""

    @staticmethod
    def _extract_finish_reason(event: Any) -> str:
        reason = getattr(event, "finish_reason", None)
        if isinstance(reason, str):
            return reason
        if isinstance(event, Mapping):
            event_map = cast(Mapping[str, Any], event)
            mapped = event_map.get("finish_reason")
            if isinstance(mapped, str):
                return mapped
        return ""

    def build_messages(
        self,
        prompt: str,
        *,
        structured_messages: list[Message] | None = None,
    ) -> list[dict[str, Any]]:
        """Build the messages list for the chat completions API.

        If *structured_messages* are provided they are converted and
        prepended (after system prompt) before the current prompt.
        """
        messages: list[dict[str, Any]] = []
        if self._system_prompt:
            messages.append({"role": "system", "content": self._system_prompt})

        # Structured multi-turn messages (from caller)
        if structured_messages:
            messages.extend(self._convert_messages(structured_messages))

        # Conversation history from active session
        if self._active_session and self._active_session in self._conversations:
            messages.extend(
                [m.to_dict() for m in self._conversations[self._active_session]]
            )

        messages.append({"role": "user", "content": prompt})
        return messages

    def build_create_kwargs(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        """Build kwargs for chat.completions.create, including tool defs."""
        params = CompletionParams.from_kwargs(kwargs)
        result: dict[str, Any] = params.to_dict()

        # Convert structured ToolChoice if provided
        tool_choice = kwargs.get("tool_choice")
        if isinstance(tool_choice, ToolChoice):
            result["tool_choice"] = self._convert_tool_choice(tool_choice)
        elif tool_choice is not None:
            result["tool_choice"] = tool_choice

        # Register tools as OpenAI function calling format
        if self._tools:
            tool_defs = [
                ToolCallDefinition(
                    t.name, t.description, t.parameters
                ).to_openai_function()
                for t in self._tools
            ]
            result["tools"] = tool_defs

        return result

    @staticmethod
    def _convert_tool_choice(choice: ToolChoice) -> Any:
        """Convert a unified ToolChoice to OpenAI format."""
        if choice.mode == "auto":
            return "auto"
        if choice.mode == "none":
            return "none"
        if choice.mode == "required":
            return "required"
        if choice.mode == "function":
            return {
                "type": "function",
                "function": {"name": choice.function_name},
            }
        return "auto"

    @staticmethod
    def _convert_messages(messages: list[Message]) -> list[dict[str, Any]]:
        """Convert unified Message objects to OpenAI dict format."""
        result: list[dict[str, Any]] = []
        for msg in messages:
            role = msg.role.value  # "user", "assistant", "system", "tool_result"
            if role == "tool_result":
                # OpenAI uses "tool" role for tool results
                for block in msg.content:
                    if block.kind == "tool_result":
                        result.append(
                            {
                                "role": "tool",
                                "content": block.text,
                                "tool_call_id": block.tool_use_id,
                            }
                        )
                continue

            # Build content — simple text or list of blocks
            text_parts = [b.text for b in msg.content if b.kind == "text"]
            content: str | list[dict[str, Any]] = (
                text_parts[0] if len(text_parts) == 1 else "\n".join(text_parts)
            )

            d: dict[str, Any] = {"role": role, "content": content}

            # Include tool_calls if present
            tool_blocks = [b for b in msg.content if b.kind == "tool_use"]
            if tool_blocks:
                import json

                d["tool_calls"] = [
                    {
                        "id": b.tool_use_id,
                        "type": "function",
                        "function": {
                            "name": b.tool_name,
                            "arguments": json.dumps(b.tool_input),
                        },
                    }
                    for b in tool_blocks
                ]

            result.append(d)
        return result

    def to_message(self, response: Any) -> Message:
        """Convert an OpenAI response to a normalized Message."""
        choice = response.choices[0]
        msg = choice.message
        blocks: list[ContentBlock] = []

        # Text content
        if msg.content:
            blocks.append(ContentBlock(kind="text", text=msg.content))

        # Tool calls
        if msg.tool_calls:
            for tc in msg.tool_calls:
                try:
                    tool_input = json.loads(tc.function.arguments)
                except (json.JSONDecodeError, TypeError):
                    tool_input = {"raw": tc.function.arguments}
                blocks.append(
                    ContentBlock(
                        kind="tool_use",
                        tool_name=tc.function.name,
                        tool_input=tool_input,
                        tool_use_id=tc.id,
                    )
                )

        if not blocks:
            blocks = [ContentBlock(kind="text", text="")]

        return Message(
            role=Role.ASSISTANT,
            content=blocks,
            raw=response,
            backend=self._backend_type,
        )


def _parse_tool_input(raw: str) -> dict[str, Any]:
    """Parse accumulated tool input delta into a dict payload."""
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return cast(dict[str, Any], parsed)
        return {"raw": raw}
    except json.JSONDecodeError:
        return {"raw": raw}


# ---------------------------------------------------------------------------
# Lazy telemetry helpers
# ---------------------------------------------------------------------------

from obscura.telemetry.traces import NoOpTracer


def _get_backend_tracer() -> Any:
    try:
        from obscura.telemetry.traces import get_tracer

        return get_tracer("obscura.openai_backend")
    except Exception:
        return NoOpTracer()


def _set_span_attr(span: Any, key: str, value: Any) -> None:
    try:
        if hasattr(span, "set_attribute"):
            span.set_attribute(key, value)
    except Exception:
        pass
