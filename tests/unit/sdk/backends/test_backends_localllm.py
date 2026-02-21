"""Comprehensive tests for sdk.backends.localllm — LocalLLMBackend.

Covers initialization, lifecycle (start/stop), send, stream, sessions,
tools, and hooks.  All OpenAI SDK interactions are mocked so tests run
without a local LLM server.
"""

from __future__ import annotations

from typing import Any, AsyncIterator
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
    mock_client: Any = AsyncMock()
    b.set_client_for_testing(mock_client)
    return b


async def _aiter(*items: Any) -> AsyncIterator[Any]:
    """Async iterator helper for mocking streaming responses."""
    for item in items:
        yield item


def _mock_completion(
    content: str | None = "hello", tool_calls: Any = None
) -> MagicMock:
    """Build a mock chat.completions.create response."""
    msg: Any = MagicMock()
    msg.content = content
    msg.tool_calls = tool_calls

    choice: Any = MagicMock()
    choice.message = msg

    resp: Any = MagicMock()
    resp.choices = [choice]
    return resp


def _mock_stream_chunk(
    content: str | None = None, tool_calls: Any = None
) -> MagicMock:
    """Build a single mock streaming chunk."""
    delta: Any = MagicMock()
    delta.content = content
    delta.tool_calls = tool_calls

    choice: Any = MagicMock()
    choice.delta = delta

    chunk: Any = MagicMock()
    chunk.choices = [choice]
    return chunk


# ===================================================================
# 1. Initialization
# ===================================================================


class TestLocalLLMInit:
    """Verify constructor defaults and parameter storage."""

    def test_default_base_url(self) -> None:
        b = LocalLLMBackend(_auth())
        assert b.base_url == "http://localhost:1234/v1"

    def test_custom_base_url(self) -> None:
        b = LocalLLMBackend(_auth("http://custom:9999/v1"))
        assert b.base_url == "http://custom:9999/v1"

    def test_model_stored(self) -> None:
        b = LocalLLMBackend(_auth(), model="llama-3")
        assert b.model == "llama-3"

    def test_model_defaults_none(self) -> None:
        b = LocalLLMBackend(_auth())
        assert b.model is None

    def test_system_prompt_stored(self) -> None:
        b = LocalLLMBackend(_auth(), system_prompt="Be helpful")
        assert b.system_prompt == "Be helpful"

    def test_system_prompt_defaults_empty(self) -> None:
        b = LocalLLMBackend(_auth())
        assert b.system_prompt == ""

    def test_empty_tools_and_hooks_on_init(self) -> None:
        b = LocalLLMBackend(_auth())
        assert b.tools == []
        assert all(len(cbs) == 0 for cbs in b.hooks.values())
        assert len(b.tool_registry) == 0

    def test_client_none_before_start(self) -> None:
        b = LocalLLMBackend(_auth())
        assert b.client is None

    def test_conversations_empty_on_init(self) -> None:
        b = LocalLLMBackend(_auth())
        assert b.conversations == {}
        assert b.active_session is None

    def test_mcp_servers_default_empty(self) -> None:
        b = LocalLLMBackend(_auth())
        assert b._mcp_servers == []  # type: ignore[reportPrivateUsage]

    def test_mcp_servers_stored(self) -> None:
        servers: list[dict[str, Any]] = [{"url": "http://mcp:8080"}]
        b = LocalLLMBackend(_auth(), mcp_servers=servers)
        expected = [MCPServerConfig.from_dict(s) for s in servers]
        assert b._mcp_servers == expected  # type: ignore[reportPrivateUsage]


# ===================================================================
# 2. Lifecycle
# ===================================================================


class TestLocalLLMLifecycle:
    """Verify start(), stop(), and client creation behaviour."""

    @pytest.mark.asyncio
    async def test_start_creates_client(self) -> None:
        b = LocalLLMBackend(_auth(), model="test-model")
        mock_client: Any = AsyncMock()
        mock_client.models.list.return_value = MagicMock(data=[])

        with patch("openai.AsyncOpenAI", return_value=mock_client) as ctor:
            await b.start()
            ctor.assert_called_once_with(
                base_url="http://localhost:1234/v1",
                api_key="not-needed",
            )
            assert b.client is mock_client

    @pytest.mark.asyncio
    async def test_start_discovers_model(self) -> None:
        b = LocalLLMBackend(_auth(), model=None)
        mock_client: Any = AsyncMock()
        mock_model: Any = MagicMock(id="llama-3")
        mock_client.models.list.return_value = MagicMock(data=[mock_model])

        with patch("openai.AsyncOpenAI", return_value=mock_client):
            await b.start()
            assert b.model == "llama-3"

    @pytest.mark.asyncio
    async def test_start_discovery_no_models(self) -> None:
        """When the server lists zero models, _model stays None."""
        b = LocalLLMBackend(_auth(), model=None)
        mock_client: Any = AsyncMock()
        mock_client.models.list.return_value = MagicMock(data=[])

        with patch("openai.AsyncOpenAI", return_value=mock_client):
            await b.start()
            assert b.model is None

    @pytest.mark.asyncio
    async def test_start_discovery_exception_returns_none(self) -> None:
        """If models.list() throws, _model stays None instead of crashing."""
        b = LocalLLMBackend(_auth(), model=None)
        mock_client: Any = AsyncMock()
        mock_client.models.list.side_effect = Exception("connection refused")

        with patch("openai.AsyncOpenAI", return_value=mock_client):
            await b.start()
            assert b.model is None

    @pytest.mark.asyncio
    async def test_start_skips_discovery_when_model_set(self) -> None:
        b = LocalLLMBackend(_auth(), model="my-model")
        mock_client: Any = AsyncMock()

        with patch("openai.AsyncOpenAI", return_value=mock_client):
            await b.start()
            mock_client.models.list.assert_not_called()
            assert b.model == "my-model"

    @pytest.mark.asyncio
    async def test_stop_closes_client(self) -> None:
        b = _backend()
        client_ref: Any = b.client
        await b.stop()
        client_ref.close.assert_awaited_once()
        assert b.client is None

    @pytest.mark.asyncio
    async def test_stop_when_no_client(self) -> None:
        """Calling stop() before start() should not raise."""
        b = LocalLLMBackend(_auth())
        await b.stop()  # no error

    @pytest.mark.asyncio
    async def test_ensure_client_raises(self) -> None:
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
    async def test_send_returns_message(self) -> None:
        b = _backend()
        client: Any = b.client
        client.chat.completions.create.return_value = _mock_completion("hello")

        msg = await b.send("Hi")

        assert isinstance(msg, Message)
        assert msg.role == Role.ASSISTANT
        assert msg.text == "hello"
        assert msg.backend == Backend.LOCALLLM

    @pytest.mark.asyncio
    async def test_send_with_tool_calls(self) -> None:
        func_mock: Any = MagicMock()
        func_mock.name = "get_weather"
        func_mock.arguments = '{"city":"NY"}'
        tc: Any = MagicMock(id="tc1", function=func_mock)
        b = _backend()
        client: Any = b.client
        client.chat.completions.create.return_value = _mock_completion(
            content=None, tool_calls=[tc]
        )

        msg = await b.send("What is the weather?")

        tool_blocks = [bl for bl in msg.content if bl.kind == "tool_use"]
        assert len(tool_blocks) == 1
        assert tool_blocks[0].tool_name == "get_weather"
        assert tool_blocks[0].tool_input == {"city": "NY"}
        assert tool_blocks[0].tool_use_id == "tc1"

    @pytest.mark.asyncio
    async def test_send_with_system_prompt(self) -> None:
        b = _backend(system_prompt="Be concise")
        client: Any = b.client
        client.chat.completions.create.return_value = _mock_completion("ok")

        await b.send("Hi")

        call_kwargs: Any = client.chat.completions.create.call_args
        messages: Any = call_kwargs.kwargs.get("messages") or call_kwargs[1].get("messages")
        assert messages[0] == {"role": "system", "content": "Be concise"}

    @pytest.mark.asyncio
    async def test_send_without_system_prompt(self) -> None:
        b = _backend(system_prompt="")
        client: Any = b.client
        client.chat.completions.create.return_value = _mock_completion("ok")

        await b.send("Hi")

        call_kwargs: Any = client.chat.completions.create.call_args
        messages: Any = call_kwargs.kwargs.get("messages") or call_kwargs[1].get("messages")
        # No system message — first message should be the user prompt
        assert messages[0] == {"role": "user", "content": "Hi"}

    @pytest.mark.asyncio
    async def test_send_empty_content_returns_empty_text_block(self) -> None:
        """When response.content is None and no tool_calls, default to empty text block."""
        b = _backend()
        client: Any = b.client
        client.chat.completions.create.return_value = _mock_completion(
            content=None, tool_calls=None
        )

        msg = await b.send("hello")
        assert len(msg.content) == 1
        assert msg.content[0].kind == "text"
        assert msg.content[0].text == ""

    @pytest.mark.asyncio
    async def test_send_passes_model(self) -> None:
        b = _backend(model="my-llama")
        client: Any = b.client
        client.chat.completions.create.return_value = _mock_completion("ok")

        await b.send("Hi")

        call_kwargs: Any = client.chat.completions.create.call_args
        assert call_kwargs.kwargs.get("model") == "my-llama"

    @pytest.mark.asyncio
    async def test_send_passes_kwargs(self) -> None:
        """Extra kwargs like temperature are forwarded via _build_create_kwargs."""
        b = _backend()
        client: Any = b.client
        client.chat.completions.create.return_value = _mock_completion("ok")

        await b.send("Hi", temperature=0.5, max_tokens=100)

        call_kwargs: Any = client.chat.completions.create.call_args
        assert call_kwargs.kwargs.get("temperature") == 0.5
        assert call_kwargs.kwargs.get("max_tokens") == 100

    @pytest.mark.asyncio
    async def test_send_tool_call_invalid_json(self) -> None:
        """When tool_calls arguments are not valid JSON, fall back to raw."""
        func_mock: Any = MagicMock()
        func_mock.name = "broken"
        func_mock.arguments = "not-json"
        tc: Any = MagicMock(id="tc2", function=func_mock)
        b = _backend()
        client: Any = b.client
        client.chat.completions.create.return_value = _mock_completion(
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
    async def test_stream_yields_text(self) -> None:
        b = _backend()
        chunk: Any = _mock_stream_chunk(content="hello")

        async def mock_response() -> AsyncIterator[Any]:
            async for item in _aiter(chunk):
                yield item

        client: Any = b.client
        client.chat.completions.create.return_value = mock_response()

        chunks: list[Any] = []
        async for c in b.stream("Hi"):
            chunks.append(c)

        text_chunks = [c for c in chunks if c.kind == ChunkKind.TEXT_DELTA]
        assert len(text_chunks) == 1
        assert text_chunks[0].text == "hello"

    @pytest.mark.asyncio
    async def test_stream_yields_done(self) -> None:
        b = _backend()
        chunk: Any = _mock_stream_chunk(content="word")

        async def mock_response() -> AsyncIterator[Any]:
            async for item in _aiter(chunk):
                yield item

        client: Any = b.client
        client.chat.completions.create.return_value = mock_response()

        chunks: list[Any] = []
        async for c in b.stream("Hi"):
            chunks.append(c)

        assert chunks[-1].kind == ChunkKind.DONE

    @pytest.mark.asyncio
    async def test_stream_multiple_text_chunks(self) -> None:
        b = _backend()
        c1: Any = _mock_stream_chunk(content="Hello")
        c2: Any = _mock_stream_chunk(content=" world")

        async def mock_response() -> AsyncIterator[Any]:
            async for item in _aiter(c1, c2):
                yield item

        client: Any = b.client
        client.chat.completions.create.return_value = mock_response()

        chunks: list[Any] = []
        async for c in b.stream("Hi"):
            chunks.append(c)

        text_chunks = [c for c in chunks if c.kind == ChunkKind.TEXT_DELTA]
        assert len(text_chunks) == 2
        assert text_chunks[0].text == "Hello"
        assert text_chunks[1].text == " world"

    @pytest.mark.asyncio
    async def test_stream_tool_calls(self) -> None:
        b = _backend()

        func_mock: Any = MagicMock()
        func_mock.name = "get_weather"
        func_mock.arguments = '{"city":"NY"}'
        tc_delta: Any = MagicMock()
        tc_delta.function = func_mock

        chunk: Any = _mock_stream_chunk(tool_calls=[tc_delta])

        async def mock_response() -> AsyncIterator[Any]:
            async for item in _aiter(chunk):
                yield item

        client: Any = b.client
        client.chat.completions.create.return_value = mock_response()

        chunks: list[Any] = []
        async for c in b.stream("weather"):
            chunks.append(c)

        tool_start_chunks = [c for c in chunks if c.kind == ChunkKind.TOOL_USE_START]
        tool_delta_chunks = [c for c in chunks if c.kind == ChunkKind.TOOL_USE_DELTA]
        assert len(tool_start_chunks) == 1
        assert tool_start_chunks[0].tool_name == "get_weather"
        assert len(tool_delta_chunks) == 1
        assert tool_delta_chunks[0].tool_input_delta == '{"city":"NY"}'

    @pytest.mark.asyncio
    async def test_stream_empty_choices_skipped(self) -> None:
        """Chunks with empty choices list should be silently skipped."""
        b = _backend()

        empty_chunk: Any = MagicMock()
        empty_chunk.choices = []

        text_chunk: Any = _mock_stream_chunk(content="ok")

        async def mock_response() -> AsyncIterator[Any]:
            async for item in _aiter(empty_chunk, text_chunk):
                yield item

        client: Any = b.client
        client.chat.completions.create.return_value = mock_response()

        chunks: list[Any] = []
        async for c in b.stream("Hi"):
            chunks.append(c)

        text_chunks = [c for c in chunks if c.kind == ChunkKind.TEXT_DELTA]
        assert len(text_chunks) == 1
        assert text_chunks[0].text == "ok"

    @pytest.mark.asyncio
    async def test_stream_sets_stream_true(self) -> None:
        """Verify stream=True is passed to the API call."""
        b = _backend()

        async def mock_response() -> AsyncIterator[Any]:
            return
            yield  # make it an async generator

        client: Any = b.client
        client.chat.completions.create.return_value = mock_response()

        async for _ in b.stream("Hi"):
            pass

        call_kwargs: Any = client.chat.completions.create.call_args
        assert call_kwargs.kwargs.get("stream") is True


# ===================================================================
# 5. Sessions
# ===================================================================


class TestLocalLLMSessions:
    """Tests for session lifecycle: create, resume, list, delete."""

    @pytest.mark.asyncio
    async def test_create_session(self) -> None:
        b = _backend()
        ref = await b.create_session()

        assert isinstance(ref, SessionRef)
        assert ref.backend == Backend.LOCALLLM
        assert b.active_session == ref.session_id
        assert ref.session_id in b.conversations

    @pytest.mark.asyncio
    async def test_resume_session(self) -> None:
        b = _backend()
        ref = await b.create_session()
        b._active_session = None  # type: ignore[reportPrivateUsage]  # simulate switching away

        await b.resume_session(ref)
        assert b.active_session == ref.session_id

    @pytest.mark.asyncio
    async def test_resume_nonexistent_raises(self) -> None:
        b = _backend()
        fake = SessionRef(session_id="nonexistent-id", backend=Backend.LOCALLLM)
        with pytest.raises(RuntimeError, match="not found"):
            await b.resume_session(fake)

    @pytest.mark.asyncio
    async def test_list_sessions(self) -> None:
        b = _backend()
        ref1 = await b.create_session()
        ref2 = await b.create_session()

        sessions = await b.list_sessions()
        session_ids = {s.session_id for s in sessions}
        assert ref1.session_id in session_ids
        assert ref2.session_id in session_ids

    @pytest.mark.asyncio
    async def test_list_sessions_empty(self) -> None:
        b = _backend()
        sessions = await b.list_sessions()
        assert sessions == []

    @pytest.mark.asyncio
    async def test_delete_session(self) -> None:
        b = _backend()
        ref = await b.create_session()
        await b.delete_session(ref)

        assert ref.session_id not in b.conversations
        sessions = await b.list_sessions()
        assert ref.session_id not in {s.session_id for s in sessions}

    @pytest.mark.asyncio
    async def test_delete_nonexistent_session_no_error(self) -> None:
        """Deleting a session that does not exist should not raise."""
        b = _backend()
        fake = SessionRef(session_id="ghost", backend=Backend.LOCALLLM)
        await b.delete_session(fake)  # no error

    @pytest.mark.asyncio
    async def test_fork_session_clones_history(self) -> None:
        b = _backend()
        client: Any = b.client
        client.chat.completions.create.return_value = _mock_completion("reply")

        src = await b.create_session()
        await b.send("hello")

        fork = await b.fork_session(src)
        assert fork.backend == Backend.LOCALLLM
        assert fork.session_id != src.session_id
        assert b.active_session == fork.session_id
        assert len(b.conversations[fork.session_id]) == len(b.conversations[src.session_id])

    @pytest.mark.asyncio
    async def test_fork_missing_session_raises(self) -> None:
        b = _backend()
        fake = SessionRef(session_id="ghost", backend=Backend.LOCALLLM)
        with pytest.raises(RuntimeError, match="not found"):
            await b.fork_session(fake)

    @pytest.mark.asyncio
    async def test_conversation_persistence(self) -> None:
        """After send(), conversation history is appended for the active session."""
        b = _backend()
        client: Any = b.client
        client.chat.completions.create.return_value = _mock_completion("reply")

        ref = await b.create_session()
        await b.send("hello")

        history: Any = b.conversations[ref.session_id]
        assert len(history) == 2

        def to_dict(m: Any) -> Any:
            return m.to_dict() if hasattr(m, "to_dict") else m

        assert to_dict(history[0]) == {"role": "user", "content": "hello"}
        assert to_dict(history[1]) == {"role": "assistant", "content": "reply"}

    @pytest.mark.asyncio
    async def test_conversation_history_in_messages(self) -> None:
        """Prior conversation history should be included in the messages list."""
        b = _backend()
        client: Any = b.client
        client.chat.completions.create.return_value = _mock_completion("resp1")

        await b.create_session()
        await b.send("first message")

        # Reset mock for second call
        client.chat.completions.create.return_value = _mock_completion("resp2")
        await b.send("second message")

        call_kwargs: Any = client.chat.completions.create.call_args
        messages: Any = call_kwargs.kwargs.get("messages") or call_kwargs[1].get("messages")
        # Should contain: history (user+assistant from first turn) + current user prompt
        user_messages: list[Any] = [m for m in messages if m["role"] == "user"]
        assert len(user_messages) == 2
        assert user_messages[0]["content"] == "first message"
        assert user_messages[1]["content"] == "second message"

    @pytest.mark.asyncio
    async def test_no_history_without_session(self) -> None:
        """When no session is active, send() should not persist history."""
        b = _backend()
        client: Any = b.client
        client.chat.completions.create.return_value = _mock_completion("ok")

        await b.send("hello")
        assert b.conversations == {}

    @pytest.mark.asyncio
    async def test_stream_persists_conversation(self) -> None:
        """Stream should also persist conversation history to the session."""
        b = _backend()
        c1: Any = _mock_stream_chunk(content="streamed")

        async def mock_response() -> AsyncIterator[Any]:
            async for item in _aiter(c1):
                yield item

        client: Any = b.client
        client.chat.completions.create.return_value = mock_response()

        ref = await b.create_session()

        async for _ in b.stream("prompt"):
            pass

        history: Any = b.conversations[ref.session_id]
        assert len(history) == 2

        def to_dict(m: Any) -> Any:
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
            handler=lambda arg1: arg1,  # type: ignore[reportUnknownLambdaType]
        )

    def test_register_tool(self) -> None:
        b = _backend()
        spec = self._make_spec()
        b.register_tool(spec)

        assert len(b.tools) == 1
        assert b.tools[0].name == "test_tool"
        assert "test_tool" in b.tool_registry

    def test_register_multiple_tools(self) -> None:
        b = _backend()
        b.register_tool(self._make_spec("tool_a"))
        b.register_tool(self._make_spec("tool_b"))

        assert len(b.tools) == 2
        assert "tool_a" in b.tool_registry
        assert "tool_b" in b.tool_registry

    def test_get_tool_registry(self) -> None:
        b = _backend()
        registry = b.get_tool_registry()
        assert isinstance(registry, ToolRegistry)

    def test_build_create_kwargs(self) -> None:
        """When tools are registered, _build_create_kwargs returns OpenAI format."""
        b = _backend()
        spec = self._make_spec("get_weather")
        b.register_tool(spec)

        result: dict[str, Any] = b._build_create_kwargs({})  # type: ignore[reportPrivateUsage]
        assert "tools" in result
        tools: Any = result["tools"]
        assert len(tools) == 1
        assert tools[0]["type"] == "function"
        assert tools[0]["function"]["name"] == "get_weather"
        assert tools[0]["function"]["description"] == "A test tool: get_weather"
        assert tools[0]["function"]["parameters"] == spec.parameters

    def test_build_create_kwargs_no_tools(self) -> None:
        """When no tools registered, no 'tools' key in kwargs."""
        b = _backend()
        result: dict[str, Any] = b._build_create_kwargs({})  # type: ignore[reportPrivateUsage]
        assert "tools" not in result

    def test_build_create_kwargs_filters_params(self) -> None:
        """Only valid params (temperature, max_tokens, etc.) pass through."""
        b = _backend()
        result: dict[str, Any] = b._build_create_kwargs(  # type: ignore[reportPrivateUsage]
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

    def test_build_create_kwargs_empty(self) -> None:
        """With no kwargs and no tools, returns empty dict."""
        b = _backend()
        assert b._build_create_kwargs({}) == {}  # type: ignore[reportPrivateUsage]


# ===================================================================
# 7. Hooks
# ===================================================================


class TestLocalLLMHooks:
    """Tests for hook registration and firing."""

    def test_register_hook(self) -> None:
        b = _backend()
        cb: Any = MagicMock()
        b.register_hook(HookPoint.PRE_TOOL_USE, cb)
        assert cb in b.hooks[HookPoint.PRE_TOOL_USE]

    def test_register_multiple_hooks_same_point(self) -> None:
        b = _backend()
        cb1: Any = MagicMock()
        cb2: Any = MagicMock()
        b.register_hook(HookPoint.STOP, cb1)
        b.register_hook(HookPoint.STOP, cb2)
        assert len(b.hooks[HookPoint.STOP]) == 2

    def test_register_hooks_different_points(self) -> None:
        b = _backend()
        cb1: Any = MagicMock()
        cb2: Any = MagicMock()
        b.register_hook(HookPoint.PRE_TOOL_USE, cb1)
        b.register_hook(HookPoint.STOP, cb2)
        assert cb1 in b.hooks[HookPoint.PRE_TOOL_USE]
        assert cb2 in b.hooks[HookPoint.STOP]

    @pytest.mark.asyncio
    async def test_hooks_fire_on_send(self) -> None:
        """Sync callback for USER_PROMPT_SUBMITTED fires during send()."""
        b = _backend()
        client: Any = b.client
        client.chat.completions.create.return_value = _mock_completion("ok")

        fired: list[HookContext] = []

        def on_prompt(ctx: HookContext) -> None:
            fired.append(ctx)

        b.register_hook(HookPoint.USER_PROMPT_SUBMITTED, on_prompt)
        await b.send("hello")

        assert len(fired) == 1
        assert fired[0].hook == HookPoint.USER_PROMPT_SUBMITTED
        assert fired[0].prompt == "hello"

    @pytest.mark.asyncio
    async def test_stop_hook_fires_on_send(self) -> None:
        """The STOP hook should fire after send() completes."""
        b = _backend()
        client: Any = b.client
        client.chat.completions.create.return_value = _mock_completion("ok")

        fired: list[HookContext] = []

        def on_stop(ctx: HookContext) -> None:
            fired.append(ctx)

        b.register_hook(HookPoint.STOP, on_stop)
        await b.send("hello")

        assert len(fired) == 1
        assert fired[0].hook == HookPoint.STOP

    @pytest.mark.asyncio
    async def test_async_hooks_fire(self) -> None:
        """Async callback should be awaited during hook execution."""
        b = _backend()
        client: Any = b.client
        client.chat.completions.create.return_value = _mock_completion("ok")

        fired: list[HookContext] = []

        async def async_on_prompt(ctx: HookContext) -> None:
            fired.append(ctx)

        b.register_hook(HookPoint.USER_PROMPT_SUBMITTED, async_on_prompt)
        await b.send("hello")

        assert len(fired) == 1
        assert fired[0].hook == HookPoint.USER_PROMPT_SUBMITTED
        assert fired[0].prompt == "hello"

    @pytest.mark.asyncio
    async def test_hook_exception_does_not_propagate(self) -> None:
        """If a hook callback raises, it should be swallowed and not crash send()."""
        b = _backend()
        client: Any = b.client
        client.chat.completions.create.return_value = _mock_completion("ok")

        def bad_hook(ctx: HookContext) -> None:
            raise ValueError("hook exploded")

        b.register_hook(HookPoint.USER_PROMPT_SUBMITTED, bad_hook)

        # Should not raise
        msg = await b.send("hello")
        assert msg.text == "ok"

    @pytest.mark.asyncio
    async def test_hooks_fire_on_stream(self) -> None:
        """Hooks should also fire during stream()."""
        b = _backend()

        async def mock_response() -> AsyncIterator[Any]:
            return
            yield

        client: Any = b.client
        client.chat.completions.create.return_value = mock_response()

        fired: list[HookContext] = []

        def on_prompt(ctx: HookContext) -> None:
            fired.append(ctx)

        b.register_hook(HookPoint.USER_PROMPT_SUBMITTED, on_prompt)

        async for _ in b.stream("hello"):
            pass

        assert len(fired) == 1
        assert fired[0].prompt == "hello"

    @pytest.mark.asyncio
    async def test_tool_hooks_fire_on_send(self) -> None:
        b = _backend()
        client: Any = b.client
        func_mock: Any = MagicMock()
        func_mock.name = "search"
        func_mock.arguments = '{"q":"weather"}'
        tc: Any = MagicMock(id="tc-search", function=func_mock)
        client.chat.completions.create.return_value = _mock_completion(
            content=None, tool_calls=[tc]
        )

        pre_hook: Any = MagicMock()
        post_hook: Any = MagicMock()
        b.register_hook(HookPoint.PRE_TOOL_USE, pre_hook)
        b.register_hook(HookPoint.POST_TOOL_USE, post_hook)

        await b.send("search weather")

        pre_hook.assert_called_once()
        post_hook.assert_called_once()
        pre_ctx = pre_hook.call_args.args[0]
        post_ctx = post_hook.call_args.args[0]
        assert pre_ctx.tool_name == "search"
        assert pre_ctx.tool_input == {"q": "weather"}
        assert post_ctx.tool_name == "search"
        assert post_ctx.tool_input == {"q": "weather"}

    @pytest.mark.asyncio
    async def test_tool_hooks_fire_on_stream(self) -> None:
        b = _backend()
        client: Any = b.client

        tc1: Any = MagicMock()
        tc1.id = "tc-stream"
        tc1.function.name = "search"
        tc1.function.arguments = '{"q":'
        tc2: Any = MagicMock()
        tc2.id = "tc-stream"
        tc2.function.name = None
        tc2.function.arguments = '"weather"}'

        async def mock_response() -> AsyncIterator[Any]:
            async for item in _aiter(
                _mock_stream_chunk(tool_calls=[tc1]),
                _mock_stream_chunk(tool_calls=[tc2]),
            ):
                yield item

        client.chat.completions.create.return_value = mock_response()

        pre_hook: Any = MagicMock()
        post_hook: Any = MagicMock()
        b.register_hook(HookPoint.PRE_TOOL_USE, pre_hook)
        b.register_hook(HookPoint.POST_TOOL_USE, post_hook)

        async for _ in b.stream("search weather"):
            pass

        pre_hook.assert_called_once()
        post_hook.assert_called_once()
        pre_ctx = pre_hook.call_args.args[0]
        post_ctx = post_hook.call_args.args[0]
        assert pre_ctx.tool_name == "search"
        assert post_ctx.tool_name == "search"
        assert post_ctx.tool_input == {"q": "weather"}

    @pytest.mark.asyncio
    async def test_run_hooks_with_no_callbacks(self) -> None:
        """_run_hooks should handle hook points with no registered callbacks."""
        b = _backend()
        # This should not raise
        ctx = HookContext(hook=HookPoint.PRE_TOOL_USE)
        await b._run_hooks(ctx)  # type: ignore[reportPrivateUsage]


# ===================================================================
# 8. Escape-hatch methods (list_models, health_check)
# ===================================================================


class TestLocalLLMEscapeHatch:
    """Tests for list_models() and health_check()."""

    @pytest.mark.asyncio
    async def test_list_models(self) -> None:
        b = _backend()
        m1: Any = MagicMock(id="model-a", object="model")
        m2: Any = MagicMock(id="model-b", object="model")
        client: Any = b.client
        client.models.list.return_value = MagicMock(data=[m1, m2])

        models = await b.list_models()
        assert len(models) == 2
        assert models[0] == {"id": "model-a", "object": "model"}
        assert models[1] == {"id": "model-b", "object": "model"}

    @pytest.mark.asyncio
    async def test_list_models_requires_client(self) -> None:
        b = LocalLLMBackend(_auth())
        with pytest.raises(RuntimeError, match="not started"):
            await b.list_models()

    @pytest.mark.asyncio
    async def test_health_check_healthy(self) -> None:
        b = _backend()
        client: Any = b.client
        client.models.list.return_value = MagicMock(data=[MagicMock(), MagicMock()])

        result = await b.health_check()
        assert result["status"] == "healthy"
        assert result["models_available"] == 2
        assert result["base_url"] == "http://localhost:1234/v1"

    @pytest.mark.asyncio
    async def test_health_check_unhealthy(self) -> None:
        b = _backend()
        client: Any = b.client
        client.models.list.side_effect = Exception("connection refused")

        result = await b.health_check()
        assert result["status"] == "unhealthy"
        assert "connection refused" in result["error"]
