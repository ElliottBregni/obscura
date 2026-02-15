"""Comprehensive tests for sdk.backends.localllm — LocalLLMBackend.

Covers initialization, lifecycle (start/stop), send, stream, sessions,
tools, and hooks.  All OpenAI SDK interactions are mocked so tests run
without a local LLM server.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sdk.internal.auth import AuthConfig
from sdk.internal.tools import ToolRegistry
from sdk.internal.types import (
    Backend,
    ChunkKind,
    HookContext,
    HookPoint,
    Message,
    Role,
    SessionRef,
    ToolSpec,
)
from sdk.backends.localllm import LocalLLMBackend
from sdk.backends.models import MCPServerConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _auth(base_url: str = "http://localhost:1234/v1") -> AuthConfig:
    """Shorthand for a LocalLLM AuthConfig."""
    return AuthConfig(localllm_base_url=base_url)


def _backend(
    base_url: str = "http://localhost:1234/v1",
    model: str | None = "test-model",
    system_prompt: str = "",
) -> LocalLLMBackend:
    """Create a backend with a mock client already wired in."""
    b = LocalLLMBackend(
        _auth(base_url),
        model=model,
        system_prompt=system_prompt,
    )
    b._client = AsyncMock()
    return b


async def _aiter(*items):
    """Async iterator helper for mocking streaming responses."""
    for item in items:
        yield item


def _mock_completion(content: str = "hello", tool_calls=None) -> MagicMock:
    """Build a mock chat.completions.create response."""
    msg = MagicMock()
    msg.content = content
    msg.tool_calls = tool_calls

    choice = MagicMock()
    choice.message = msg

    resp = MagicMock()
    resp.choices = [choice]
    return resp


def _mock_stream_chunk(content: str | None = None, tool_calls=None) -> MagicMock:
    """Build a single mock streaming chunk."""
    delta = MagicMock()
    delta.content = content
    delta.tool_calls = tool_calls

    choice = MagicMock()
    choice.delta = delta

    chunk = MagicMock()
    chunk.choices = [choice]
    return chunk


# ===================================================================
# 1. Initialization
# ===================================================================


class TestLocalLLMInit:
    """Verify constructor defaults and parameter storage."""

    def test_default_base_url(self):
        b = LocalLLMBackend(_auth())
        assert b._base_url == "http://localhost:1234/v1"

    def test_custom_base_url(self):
        b = LocalLLMBackend(_auth("http://custom:9999/v1"))
        assert b._base_url == "http://custom:9999/v1"

    def test_model_stored(self):
        b = LocalLLMBackend(_auth(), model="llama-3")
        assert b._model == "llama-3"

    def test_model_defaults_none(self):
        b = LocalLLMBackend(_auth())
        assert b._model is None

    def test_system_prompt_stored(self):
        b = LocalLLMBackend(_auth(), system_prompt="Be helpful")
        assert b._system_prompt == "Be helpful"

    def test_system_prompt_defaults_empty(self):
        b = LocalLLMBackend(_auth())
        assert b._system_prompt == ""

    def test_empty_tools_and_hooks_on_init(self):
        b = LocalLLMBackend(_auth())
        assert b._tools == []
        assert all(len(cbs) == 0 for cbs in b._hooks.values())
        assert len(b._tool_registry) == 0

    def test_client_none_before_start(self):
        b = LocalLLMBackend(_auth())
        assert b._client is None

    def test_conversations_empty_on_init(self):
        b = LocalLLMBackend(_auth())
        assert b._conversations == {}
        assert b._active_session is None

    def test_mcp_servers_default_empty(self):
        b = LocalLLMBackend(_auth())
        assert b._mcp_servers == []

    def test_mcp_servers_stored(self):
        servers = [{"url": "http://mcp:8080"}]
        b = LocalLLMBackend(_auth(), mcp_servers=servers)
        expected = [MCPServerConfig.from_dict(s) for s in servers]
        assert b._mcp_servers == expected


# ===================================================================
# 2. Lifecycle
# ===================================================================


class TestLocalLLMLifecycle:
    """Verify start(), stop(), and client creation behaviour."""

    @pytest.mark.asyncio
    async def test_start_creates_client(self):
        b = LocalLLMBackend(_auth(), model="test-model")
        mock_client = AsyncMock()
        mock_client.models.list.return_value = MagicMock(data=[])

        with patch("openai.AsyncOpenAI", return_value=mock_client) as ctor:
            await b.start()
            ctor.assert_called_once_with(
                base_url="http://localhost:1234/v1",
                api_key="not-needed",
            )
            assert b._client is mock_client

    @pytest.mark.asyncio
    async def test_start_discovers_model(self):
        b = LocalLLMBackend(_auth(), model=None)
        mock_client = AsyncMock()
        mock_model = MagicMock(id="llama-3")
        mock_client.models.list.return_value = MagicMock(data=[mock_model])

        with patch("openai.AsyncOpenAI", return_value=mock_client):
            await b.start()
            assert b._model == "llama-3"

    @pytest.mark.asyncio
    async def test_start_discovery_no_models(self):
        """When the server lists zero models, _model stays None."""
        b = LocalLLMBackend(_auth(), model=None)
        mock_client = AsyncMock()
        mock_client.models.list.return_value = MagicMock(data=[])

        with patch("openai.AsyncOpenAI", return_value=mock_client):
            await b.start()
            assert b._model is None

    @pytest.mark.asyncio
    async def test_start_discovery_exception_returns_none(self):
        """If models.list() throws, _model stays None instead of crashing."""
        b = LocalLLMBackend(_auth(), model=None)
        mock_client = AsyncMock()
        mock_client.models.list.side_effect = Exception("connection refused")

        with patch("openai.AsyncOpenAI", return_value=mock_client):
            await b.start()
            assert b._model is None

    @pytest.mark.asyncio
    async def test_start_skips_discovery_when_model_set(self):
        b = LocalLLMBackend(_auth(), model="my-model")
        mock_client = AsyncMock()

        with patch("openai.AsyncOpenAI", return_value=mock_client):
            await b.start()
            mock_client.models.list.assert_not_called()
            assert b._model == "my-model"

    @pytest.mark.asyncio
    async def test_stop_closes_client(self):
        b = _backend()
        client_ref = b._client
        await b.stop()
        client_ref.close.assert_awaited_once()
        assert b._client is None

    @pytest.mark.asyncio
    async def test_stop_when_no_client(self):
        """Calling stop() before start() should not raise."""
        b = LocalLLMBackend(_auth())
        await b.stop()  # no error

    @pytest.mark.asyncio
    async def test_ensure_client_raises(self):
        """Calling send() without start() raises RuntimeError."""
        b = LocalLLMBackend(_auth())
        with pytest.raises(RuntimeError, match="not started"):
            await b.send("hello")


# ===================================================================
# 3. Send
# ===================================================================


class TestLocalLLMSend:
    """Tests for the non-streaming send() method."""

    @pytest.mark.asyncio
    async def test_send_returns_message(self):
        b = _backend()
        b._client.chat.completions.create.return_value = _mock_completion("hello")

        msg = await b.send("Hi")

        assert isinstance(msg, Message)
        assert msg.role == Role.ASSISTANT
        assert msg.text == "hello"
        assert msg.backend == Backend.LOCALLLM

    @pytest.mark.asyncio
    async def test_send_with_tool_calls(self):
        func_mock = MagicMock()
        func_mock.name = "get_weather"
        func_mock.arguments = '{"city":"NY"}'
        tc = MagicMock(id="tc1", function=func_mock)
        b = _backend()
        b._client.chat.completions.create.return_value = _mock_completion(
            content=None, tool_calls=[tc]
        )

        msg = await b.send("What is the weather?")

        tool_blocks = [bl for bl in msg.content if bl.kind == "tool_use"]
        assert len(tool_blocks) == 1
        assert tool_blocks[0].tool_name == "get_weather"
        assert tool_blocks[0].tool_input == {"city": "NY"}
        assert tool_blocks[0].tool_use_id == "tc1"

    @pytest.mark.asyncio
    async def test_send_with_system_prompt(self):
        b = _backend(system_prompt="Be concise")
        b._client.chat.completions.create.return_value = _mock_completion("ok")

        await b.send("Hi")

        call_kwargs = b._client.chat.completions.create.call_args
        messages = call_kwargs.kwargs.get("messages") or call_kwargs[1].get("messages")
        assert messages[0] == {"role": "system", "content": "Be concise"}

    @pytest.mark.asyncio
    async def test_send_without_system_prompt(self):
        b = _backend(system_prompt="")
        b._client.chat.completions.create.return_value = _mock_completion("ok")

        await b.send("Hi")

        call_kwargs = b._client.chat.completions.create.call_args
        messages = call_kwargs.kwargs.get("messages") or call_kwargs[1].get("messages")
        # No system message — first message should be the user prompt
        assert messages[0] == {"role": "user", "content": "Hi"}

    @pytest.mark.asyncio
    async def test_send_empty_content_returns_empty_text_block(self):
        """When response.content is None and no tool_calls, default to empty text block."""
        b = _backend()
        b._client.chat.completions.create.return_value = _mock_completion(
            content=None, tool_calls=None
        )

        msg = await b.send("hello")
        assert len(msg.content) == 1
        assert msg.content[0].kind == "text"
        assert msg.content[0].text == ""

    @pytest.mark.asyncio
    async def test_send_passes_model(self):
        b = _backend(model="my-llama")
        b._client.chat.completions.create.return_value = _mock_completion("ok")

        await b.send("Hi")

        call_kwargs = b._client.chat.completions.create.call_args
        assert call_kwargs.kwargs.get("model") == "my-llama"

    @pytest.mark.asyncio
    async def test_send_passes_kwargs(self):
        """Extra kwargs like temperature are forwarded via _build_create_kwargs."""
        b = _backend()
        b._client.chat.completions.create.return_value = _mock_completion("ok")

        await b.send("Hi", temperature=0.5, max_tokens=100)

        call_kwargs = b._client.chat.completions.create.call_args
        assert call_kwargs.kwargs.get("temperature") == 0.5
        assert call_kwargs.kwargs.get("max_tokens") == 100

    @pytest.mark.asyncio
    async def test_send_tool_call_invalid_json(self):
        """When tool_calls arguments are not valid JSON, fall back to raw."""
        func_mock = MagicMock()
        func_mock.name = "broken"
        func_mock.arguments = "not-json"
        tc = MagicMock(id="tc2", function=func_mock)
        b = _backend()
        b._client.chat.completions.create.return_value = _mock_completion(
            content=None, tool_calls=[tc]
        )

        msg = await b.send("test")
        tool_blocks = [bl for bl in msg.content if bl.kind == "tool_use"]
        assert len(tool_blocks) == 1
        assert tool_blocks[0].tool_input == {"raw": "not-json"}


# ===================================================================
# 4. Stream
# ===================================================================


class TestLocalLLMStream:
    """Tests for the streaming stream() method."""

    @pytest.mark.asyncio
    async def test_stream_yields_text(self):
        b = _backend()
        chunk = _mock_stream_chunk(content="hello")

        async def mock_response():
            async for item in _aiter(chunk):
                yield item

        b._client.chat.completions.create.return_value = mock_response()

        chunks = []
        async for c in b.stream("Hi"):
            chunks.append(c)

        text_chunks = [c for c in chunks if c.kind == ChunkKind.TEXT_DELTA]
        assert len(text_chunks) == 1
        assert text_chunks[0].text == "hello"

    @pytest.mark.asyncio
    async def test_stream_yields_done(self):
        b = _backend()
        chunk = _mock_stream_chunk(content="word")

        async def mock_response():
            async for item in _aiter(chunk):
                yield item

        b._client.chat.completions.create.return_value = mock_response()

        chunks = []
        async for c in b.stream("Hi"):
            chunks.append(c)

        assert chunks[-1].kind == ChunkKind.DONE

    @pytest.mark.asyncio
    async def test_stream_multiple_text_chunks(self):
        b = _backend()
        c1 = _mock_stream_chunk(content="Hello")
        c2 = _mock_stream_chunk(content=" world")

        async def mock_response():
            async for item in _aiter(c1, c2):
                yield item

        b._client.chat.completions.create.return_value = mock_response()

        chunks = []
        async for c in b.stream("Hi"):
            chunks.append(c)

        text_chunks = [c for c in chunks if c.kind == ChunkKind.TEXT_DELTA]
        assert len(text_chunks) == 2
        assert text_chunks[0].text == "Hello"
        assert text_chunks[1].text == " world"

    @pytest.mark.asyncio
    async def test_stream_tool_calls(self):
        b = _backend()

        func_mock = MagicMock()
        func_mock.name = "get_weather"
        func_mock.arguments = '{"city":"NY"}'
        tc_delta = MagicMock()
        tc_delta.function = func_mock

        chunk = _mock_stream_chunk(tool_calls=[tc_delta])

        async def mock_response():
            async for item in _aiter(chunk):
                yield item

        b._client.chat.completions.create.return_value = mock_response()

        chunks = []
        async for c in b.stream("weather"):
            chunks.append(c)

        tool_start_chunks = [c for c in chunks if c.kind == ChunkKind.TOOL_USE_START]
        tool_delta_chunks = [c for c in chunks if c.kind == ChunkKind.TOOL_USE_DELTA]
        assert len(tool_start_chunks) == 1
        assert tool_start_chunks[0].tool_name == "get_weather"
        assert len(tool_delta_chunks) == 1
        assert tool_delta_chunks[0].tool_input_delta == '{"city":"NY"}'

    @pytest.mark.asyncio
    async def test_stream_empty_choices_skipped(self):
        """Chunks with empty choices list should be silently skipped."""
        b = _backend()

        empty_chunk = MagicMock()
        empty_chunk.choices = []

        text_chunk = _mock_stream_chunk(content="ok")

        async def mock_response():
            async for item in _aiter(empty_chunk, text_chunk):
                yield item

        b._client.chat.completions.create.return_value = mock_response()

        chunks = []
        async for c in b.stream("Hi"):
            chunks.append(c)

        text_chunks = [c for c in chunks if c.kind == ChunkKind.TEXT_DELTA]
        assert len(text_chunks) == 1
        assert text_chunks[0].text == "ok"

    @pytest.mark.asyncio
    async def test_stream_sets_stream_true(self):
        """Verify stream=True is passed to the API call."""
        b = _backend()

        async def mock_response():
            return
            yield  # make it an async generator

        b._client.chat.completions.create.return_value = mock_response()

        async for _ in b.stream("Hi"):
            pass

        call_kwargs = b._client.chat.completions.create.call_args
        assert call_kwargs.kwargs.get("stream") is True


# ===================================================================
# 5. Sessions
# ===================================================================


class TestLocalLLMSessions:
    """Tests for session lifecycle: create, resume, list, delete."""

    @pytest.mark.asyncio
    async def test_create_session(self):
        b = _backend()
        ref = await b.create_session()

        assert isinstance(ref, SessionRef)
        assert ref.backend == Backend.LOCALLLM
        assert b._active_session == ref.session_id
        assert ref.session_id in b._conversations

    @pytest.mark.asyncio
    async def test_resume_session(self):
        b = _backend()
        ref = await b.create_session()
        b._active_session = None  # simulate switching away

        await b.resume_session(ref)
        assert b._active_session == ref.session_id

    @pytest.mark.asyncio
    async def test_resume_nonexistent_raises(self):
        b = _backend()
        fake = SessionRef(session_id="nonexistent-id", backend=Backend.LOCALLLM)
        with pytest.raises(RuntimeError, match="not found"):
            await b.resume_session(fake)

    @pytest.mark.asyncio
    async def test_list_sessions(self):
        b = _backend()
        ref1 = await b.create_session()
        ref2 = await b.create_session()

        sessions = await b.list_sessions()
        session_ids = {s.session_id for s in sessions}
        assert ref1.session_id in session_ids
        assert ref2.session_id in session_ids

    @pytest.mark.asyncio
    async def test_list_sessions_empty(self):
        b = _backend()
        sessions = await b.list_sessions()
        assert sessions == []

    @pytest.mark.asyncio
    async def test_delete_session(self):
        b = _backend()
        ref = await b.create_session()
        await b.delete_session(ref)

        assert ref.session_id not in b._conversations
        sessions = await b.list_sessions()
        assert ref.session_id not in {s.session_id for s in sessions}

    @pytest.mark.asyncio
    async def test_delete_nonexistent_session_no_error(self):
        """Deleting a session that does not exist should not raise."""
        b = _backend()
        fake = SessionRef(session_id="ghost", backend=Backend.LOCALLLM)
        await b.delete_session(fake)  # no error

    @pytest.mark.asyncio
    async def test_conversation_persistence(self):
        """After send(), conversation history is appended for the active session."""
        b = _backend()
        b._client.chat.completions.create.return_value = _mock_completion("reply")

        ref = await b.create_session()
        await b.send("hello")

        history = b._conversations[ref.session_id]
        assert len(history) == 2
        def to_dict(m):  # type: ignore[no-untyped-def]
            return m.to_dict() if hasattr(m, "to_dict") else m

        assert to_dict(history[0]) == {"role": "user", "content": "hello"}
        assert to_dict(history[1]) == {"role": "assistant", "content": "reply"}

    @pytest.mark.asyncio
    async def test_conversation_history_in_messages(self):
        """Prior conversation history should be included in the messages list."""
        b = _backend()
        b._client.chat.completions.create.return_value = _mock_completion("resp1")

        await b.create_session()
        await b.send("first message")

        # Reset mock for second call
        b._client.chat.completions.create.return_value = _mock_completion("resp2")
        await b.send("second message")

        call_kwargs = b._client.chat.completions.create.call_args
        messages = call_kwargs.kwargs.get("messages") or call_kwargs[1].get("messages")
        # Should contain: history (user+assistant from first turn) + current user prompt
        user_messages = [m for m in messages if m["role"] == "user"]
        assert len(user_messages) == 2
        assert user_messages[0]["content"] == "first message"
        assert user_messages[1]["content"] == "second message"

    @pytest.mark.asyncio
    async def test_no_history_without_session(self):
        """When no session is active, send() should not persist history."""
        b = _backend()
        b._client.chat.completions.create.return_value = _mock_completion("ok")

        await b.send("hello")
        assert b._conversations == {}

    @pytest.mark.asyncio
    async def test_stream_persists_conversation(self):
        """Stream should also persist conversation history to the session."""
        b = _backend()
        c1 = _mock_stream_chunk(content="streamed")

        async def mock_response():
            async for item in _aiter(c1):
                yield item

        b._client.chat.completions.create.return_value = mock_response()

        ref = await b.create_session()

        async for _ in b.stream("prompt"):
            pass

        history = b._conversations[ref.session_id]
        assert len(history) == 2
        def to_dict(m):  # type: ignore[no-untyped-def]
            return m.to_dict() if hasattr(m, "to_dict") else m

        assert to_dict(history[0]) == {"role": "user", "content": "prompt"}
        assert to_dict(history[1]) == {"role": "assistant", "content": "streamed"}


# ===================================================================
# 6. Tools
# ===================================================================


class TestLocalLLMTools:
    """Tests for tool registration and _build_create_kwargs output."""

    def _make_spec(self, name: str = "test_tool") -> ToolSpec:
        return ToolSpec(
            name=name,
            description=f"A test tool: {name}",
            parameters={
                "type": "object",
                "properties": {"arg1": {"type": "string"}},
                "required": ["arg1"],
            },
            handler=lambda arg1: arg1,
        )

    def test_register_tool(self):
        b = _backend()
        spec = self._make_spec()
        b.register_tool(spec)

        assert len(b._tools) == 1
        assert b._tools[0].name == "test_tool"
        assert "test_tool" in b._tool_registry

    def test_register_multiple_tools(self):
        b = _backend()
        b.register_tool(self._make_spec("tool_a"))
        b.register_tool(self._make_spec("tool_b"))

        assert len(b._tools) == 2
        assert "tool_a" in b._tool_registry
        assert "tool_b" in b._tool_registry

    def test_get_tool_registry(self):
        b = _backend()
        registry = b.get_tool_registry()
        assert isinstance(registry, ToolRegistry)

    def test_build_create_kwargs(self):
        """When tools are registered, _build_create_kwargs returns OpenAI format."""
        b = _backend()
        spec = self._make_spec("get_weather")
        b.register_tool(spec)

        result = b._build_create_kwargs({})
        assert "tools" in result
        tools = result["tools"]
        assert len(tools) == 1
        assert tools[0]["type"] == "function"
        assert tools[0]["function"]["name"] == "get_weather"
        assert tools[0]["function"]["description"] == "A test tool: get_weather"
        assert tools[0]["function"]["parameters"] == spec.parameters

    def test_build_create_kwargs_no_tools(self):
        """When no tools registered, no 'tools' key in kwargs."""
        b = _backend()
        result = b._build_create_kwargs({})
        assert "tools" not in result

    def test_build_create_kwargs_filters_params(self):
        """Only valid params (temperature, max_tokens, etc.) pass through."""
        b = _backend()
        result = b._build_create_kwargs(
            {
                "temperature": 0.7,
                "top_p": 0.9,
                "max_tokens": 500,
                "stop": ["\n"],
                "frequency_penalty": 0.1,
                "presence_penalty": 0.2,
                "seed": 42,
                # Invalid params that should be filtered out
                "invalid_param": "should_not_appear",
                "foo": "bar",
            }
        )

        assert result["temperature"] == 0.7
        assert result["top_p"] == 0.9
        assert result["max_tokens"] == 500
        assert result["stop"] == ["\n"]
        assert result["frequency_penalty"] == 0.1
        assert result["presence_penalty"] == 0.2
        assert result["seed"] == 42
        assert "invalid_param" not in result
        assert "foo" not in result

    def test_build_create_kwargs_empty(self):
        """With no kwargs and no tools, returns empty dict."""
        b = _backend()
        assert b._build_create_kwargs({}) == {}


# ===================================================================
# 7. Hooks
# ===================================================================


class TestLocalLLMHooks:
    """Tests for hook registration and firing."""

    def test_register_hook(self):
        b = _backend()
        cb = MagicMock()
        b.register_hook(HookPoint.PRE_TOOL_USE, cb)
        assert cb in b._hooks[HookPoint.PRE_TOOL_USE]

    def test_register_multiple_hooks_same_point(self):
        b = _backend()
        cb1 = MagicMock()
        cb2 = MagicMock()
        b.register_hook(HookPoint.STOP, cb1)
        b.register_hook(HookPoint.STOP, cb2)
        assert len(b._hooks[HookPoint.STOP]) == 2

    def test_register_hooks_different_points(self):
        b = _backend()
        cb1 = MagicMock()
        cb2 = MagicMock()
        b.register_hook(HookPoint.PRE_TOOL_USE, cb1)
        b.register_hook(HookPoint.STOP, cb2)
        assert cb1 in b._hooks[HookPoint.PRE_TOOL_USE]
        assert cb2 in b._hooks[HookPoint.STOP]

    @pytest.mark.asyncio
    async def test_hooks_fire_on_send(self):
        """Sync callback for USER_PROMPT_SUBMITTED fires during send()."""
        b = _backend()
        b._client.chat.completions.create.return_value = _mock_completion("ok")

        fired = []

        def on_prompt(ctx: HookContext):
            fired.append(ctx)

        b.register_hook(HookPoint.USER_PROMPT_SUBMITTED, on_prompt)
        await b.send("hello")

        assert len(fired) == 1
        assert fired[0].hook == HookPoint.USER_PROMPT_SUBMITTED
        assert fired[0].prompt == "hello"

    @pytest.mark.asyncio
    async def test_stop_hook_fires_on_send(self):
        """The STOP hook should fire after send() completes."""
        b = _backend()
        b._client.chat.completions.create.return_value = _mock_completion("ok")

        fired = []

        def on_stop(ctx: HookContext):
            fired.append(ctx)

        b.register_hook(HookPoint.STOP, on_stop)
        await b.send("hello")

        assert len(fired) == 1
        assert fired[0].hook == HookPoint.STOP

    @pytest.mark.asyncio
    async def test_async_hooks_fire(self):
        """Async callback should be awaited during hook execution."""
        b = _backend()
        b._client.chat.completions.create.return_value = _mock_completion("ok")

        fired = []

        async def async_on_prompt(ctx: HookContext):
            fired.append(ctx)

        b.register_hook(HookPoint.USER_PROMPT_SUBMITTED, async_on_prompt)
        await b.send("hello")

        assert len(fired) == 1
        assert fired[0].hook == HookPoint.USER_PROMPT_SUBMITTED
        assert fired[0].prompt == "hello"

    @pytest.mark.asyncio
    async def test_hook_exception_does_not_propagate(self):
        """If a hook callback raises, it should be swallowed and not crash send()."""
        b = _backend()
        b._client.chat.completions.create.return_value = _mock_completion("ok")

        def bad_hook(ctx: HookContext):
            raise ValueError("hook exploded")

        b.register_hook(HookPoint.USER_PROMPT_SUBMITTED, bad_hook)

        # Should not raise
        msg = await b.send("hello")
        assert msg.text == "ok"

    @pytest.mark.asyncio
    async def test_hooks_fire_on_stream(self):
        """Hooks should also fire during stream()."""
        b = _backend()

        async def mock_response():
            return
            yield

        b._client.chat.completions.create.return_value = mock_response()

        fired = []

        def on_prompt(ctx: HookContext):
            fired.append(ctx)

        b.register_hook(HookPoint.USER_PROMPT_SUBMITTED, on_prompt)

        async for _ in b.stream("hello"):
            pass

        assert len(fired) == 1
        assert fired[0].prompt == "hello"

    @pytest.mark.asyncio
    async def test_run_hooks_with_no_callbacks(self):
        """_run_hooks should handle hook points with no registered callbacks."""
        b = _backend()
        # This should not raise
        ctx = HookContext(hook=HookPoint.PRE_TOOL_USE)
        await b._run_hooks(ctx)


# ===================================================================
# 8. Escape-hatch methods (list_models, health_check)
# ===================================================================


class TestLocalLLMEscapeHatch:
    """Tests for list_models() and health_check()."""

    @pytest.mark.asyncio
    async def test_list_models(self):
        b = _backend()
        m1 = MagicMock(id="model-a", object="model")
        m2 = MagicMock(id="model-b", object="model")
        b._client.models.list.return_value = MagicMock(data=[m1, m2])

        models = await b.list_models()
        assert len(models) == 2
        assert models[0] == {"id": "model-a", "object": "model"}
        assert models[1] == {"id": "model-b", "object": "model"}

    @pytest.mark.asyncio
    async def test_list_models_requires_client(self):
        b = LocalLLMBackend(_auth())
        with pytest.raises(RuntimeError, match="not started"):
            await b.list_models()

    @pytest.mark.asyncio
    async def test_health_check_healthy(self):
        b = _backend()
        b._client.models.list.return_value = MagicMock(data=[MagicMock(), MagicMock()])

        result = await b.health_check()
        assert result["status"] == "healthy"
        assert result["models_available"] == 2
        assert result["base_url"] == "http://localhost:1234/v1"

    @pytest.mark.asyncio
    async def test_health_check_unhealthy(self):
        b = _backend()
        b._client.models.list.side_effect = Exception("connection refused")

        result = await b.health_check()
        assert result["status"] == "unhealthy"
        assert "connection refused" in result["error"]
