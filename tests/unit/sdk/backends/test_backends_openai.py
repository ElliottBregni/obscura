"""Comprehensive tests for sdk.backends.openai_compat — OpenAIBackend.

Covers initialization, lifecycle, send/stream, sessions, tools, and hooks.
All OpenAI SDK interactions are mocked via AsyncMock/MagicMock.
"""

from typing import Any, AsyncIterator

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from sdk.internal.auth import AuthConfig
from sdk.internal.types import (
    Backend,
    ChunkKind,
    HookContext,
    HookPoint,
    Message,
    Role,
    SessionRef,
    StreamChunk,
    ToolSpec,
)
from sdk.internal.tools import ToolRegistry
from sdk.backends.openai_compat import OpenAIBackend
from sdk.backends.models import ChatMessage


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _auth(**kw: Any) -> AuthConfig:
    """Build an AuthConfig for OpenAI tests."""
    return AuthConfig(
        openai_api_key=kw.get("api_key", "test-key"),
        openai_base_url=kw.get("base_url", "https://api.openai.com/v1"),
    )


async def _aiter(*items: Any) -> AsyncIterator[Any]:
    """Yield items as an async iterator (mock stream helper)."""
    for item in items:
        yield item


def _make_choice(content: str | None = "Hello", tool_calls: Any = None) -> Any:
    """Build a MagicMock response choice."""
    choice: Any = MagicMock()
    choice.message.content = content
    choice.message.tool_calls = tool_calls
    return choice


def _make_response(content: str | None = "Hello", tool_calls: Any = None) -> Any:
    """Build a full MagicMock chat completion response."""
    resp: Any = MagicMock()
    resp.choices = [_make_choice(content=content, tool_calls=tool_calls)]
    return resp


def _make_stream_chunk(content: str | None = None, tool_calls: Any = None, choices: bool = True) -> Any:
    """Build a MagicMock streaming chunk."""
    chunk: Any = MagicMock()
    if not choices:
        chunk.choices = []
        return chunk
    delta: Any = MagicMock()
    delta.content = content
    delta.tool_calls = tool_calls
    choice: Any = MagicMock()
    choice.delta = delta
    chunk.choices = [choice]
    return chunk


def _make_tool_call(name: str = "my_tool", arguments: str = '{"x": 1}', tc_id: str = "call_abc123") -> Any:
    """Build a MagicMock tool call object."""
    tc: Any = MagicMock()
    tc.id = tc_id
    tc.function.name = name
    tc.function.arguments = arguments
    return tc


def _tool_handler(**kwargs: Any) -> str:
    """Generic tool handler for testing."""
    return f"result: {kwargs.get('arg1', '')}"


def _sample_tool_spec(name: str = "test_tool") -> ToolSpec:
    """Build a simple ToolSpec for testing."""
    return ToolSpec(
        name=name,
        description=f"A tool called {name}",
        parameters={
            "type": "object",
            "properties": {"arg1": {"type": "string"}},
            "required": ["arg1"],
        },
        handler=_tool_handler,
    )


def _backend(**kw: Any) -> OpenAIBackend:
    """Create an OpenAIBackend with sensible test defaults."""
    auth = _auth(
        api_key=kw.pop("api_key", "test-key"),
        base_url=kw.pop("base_url", "https://api.openai.com/v1"),
    )
    return OpenAIBackend(auth, **kw)


# ===================================================================
# 1. TestOpenAIInit
# ===================================================================


class TestOpenAIInit:
    """Verify constructor stores config correctly."""

    def test_api_key_stored(self) -> None:
        b = _backend(api_key="sk-my-key")
        assert b.api_key == "sk-my-key"

    def test_base_url_stored(self) -> None:
        b = _backend(base_url="https://openrouter.ai/api/v1")
        assert b.base_url == "https://openrouter.ai/api/v1"

    def test_default_model(self) -> None:
        b = _backend()
        assert b.model == "gpt-4o"

    def test_custom_model(self) -> None:
        b = _backend(model="gpt-4-turbo")
        assert b.model == "gpt-4-turbo"

    def test_system_prompt_stored(self) -> None:
        b = _backend(system_prompt="You are a helpful assistant.")
        assert b.system_prompt == "You are a helpful assistant."

    def test_empty_tools_hooks_on_init(self) -> None:
        b = _backend()
        assert b.tools == []
        assert all(len(cbs) == 0 for cbs in b.hooks.values())
        assert b.client is None
        assert b.active_session is None
        assert b.conversations == {}


# ===================================================================
# 2. TestOpenAILifecycle
# ===================================================================


class TestOpenAILifecycle:
    """Verify start/stop lifecycle and client management."""

    @pytest.mark.asyncio
    async def test_start_creates_client(self) -> None:
        b = _backend(api_key="sk-lifecycle")
        mock_client: Any = AsyncMock()
        with patch("openai.AsyncOpenAI", return_value=mock_client) as MockCtor:
            await b.start()
            MockCtor.assert_called_once()
            call_kwargs: Any = MockCtor.call_args
            assert (
                call_kwargs.kwargs.get("api_key") == "sk-lifecycle"
                or call_kwargs[1].get("api_key") == "sk-lifecycle"
            )
            assert b.client is mock_client

    @pytest.mark.asyncio
    async def test_start_with_base_url(self) -> None:
        b = _backend(base_url="https://api.together.xyz/v1")
        mock_client: Any = AsyncMock()
        with patch("openai.AsyncOpenAI", return_value=mock_client) as MockCtor:
            await b.start()
            MockCtor.assert_called_once_with(
                api_key="test-key",
                base_url="https://api.together.xyz/v1",
            )

    @pytest.mark.asyncio
    async def test_start_without_base_url(self) -> None:
        auth = AuthConfig(openai_api_key="sk-nourl", openai_base_url=None)
        b = OpenAIBackend(auth)
        mock_client: Any = AsyncMock()
        with patch("openai.AsyncOpenAI", return_value=mock_client) as MockCtor:
            await b.start()
            # base_url should NOT be in kwargs when it is None
            call_kwargs: Any = MockCtor.call_args[1]
            assert "base_url" not in call_kwargs

    @pytest.mark.asyncio
    async def test_stop_closes_client(self) -> None:
        b = _backend()
        mock_client: Any = AsyncMock()
        b.set_client_for_testing(mock_client)
        await b.stop()
        mock_client.close.assert_awaited_once()
        assert b.client is None

    @pytest.mark.asyncio
    async def test_stop_noop_when_no_client(self) -> None:
        b = _backend()
        assert b.client is None
        await b.stop()  # should not raise
        assert b.client is None

    @pytest.mark.asyncio
    async def test_ensure_client_raises(self) -> None:
        b = _backend()
        with pytest.raises(RuntimeError, match="not started"):
            await b.send("hello")

    @pytest.mark.asyncio
    async def test_ensure_client_raises_on_stream(self) -> None:
        b = _backend()
        with pytest.raises(RuntimeError, match="not started"):
            async for _ in b.stream("hello"):
                pass


# ===================================================================
# 3. TestOpenAISend
# ===================================================================


class TestOpenAISend:
    """Verify send() produces correct Message objects."""

    @pytest.mark.asyncio
    async def test_send_text_response(self) -> None:
        b = _backend(model="gpt-4o-test")
        b.set_client_for_testing(AsyncMock())
        b.client.chat.completions.create.return_value = _make_response(
            content="Response from OpenAI"
        )

        msg = await b.send("Hello")

        assert isinstance(msg, Message)
        assert msg.role == Role.ASSISTANT
        assert msg.backend == Backend.OPENAI
        assert len(msg.content) == 1
        assert msg.content[0].kind == "text"
        assert msg.content[0].text == "Response from OpenAI"
        assert msg.text == "Response from OpenAI"

    @pytest.mark.asyncio
    async def test_send_with_tool_calls(self) -> None:
        b = _backend(model="gpt-4o-test")
        b.set_client_for_testing(AsyncMock())

        tc = _make_tool_call(name="read_file", arguments='{"path": "/tmp/a.txt"}')
        b.client.chat.completions.create.return_value = _make_response(
            content=None, tool_calls=[tc]
        )

        msg = await b.send("Read the file")

        tool_blocks = [bl for bl in msg.content if bl.kind == "tool_use"]
        assert len(tool_blocks) == 1
        assert tool_blocks[0].tool_name == "read_file"
        assert tool_blocks[0].tool_input == {"path": "/tmp/a.txt"}
        assert tool_blocks[0].tool_use_id == "call_abc123"

    @pytest.mark.asyncio
    async def test_send_with_tool_calls_invalid_json(self) -> None:
        """When tool arguments are not valid JSON, they are wrapped in raw."""
        b = _backend(model="gpt-4o-test")
        b.set_client_for_testing(AsyncMock())

        tc = _make_tool_call(name="calc", arguments="not-json")
        b.client.chat.completions.create.return_value = _make_response(
            content=None, tool_calls=[tc]
        )

        msg = await b.send("Calculate")

        tool_blocks = [bl for bl in msg.content if bl.kind == "tool_use"]
        assert len(tool_blocks) == 1
        assert tool_blocks[0].tool_input == {"raw": "not-json"}

    @pytest.mark.asyncio
    async def test_send_with_system_prompt(self) -> None:
        b = _backend(model="gpt-4o-test", system_prompt="Be concise.")
        b.set_client_for_testing(AsyncMock())
        b.client.chat.completions.create.return_value = _make_response(content="OK")

        await b.send("Hi")

        call_args: Any = b.client.chat.completions.create.call_args
        messages = call_args.kwargs.get("messages") or call_args[1].get("messages")
        assert messages[0] == {"role": "system", "content": "Be concise."}
        assert messages[-1] == {"role": "user", "content": "Hi"}

    @pytest.mark.asyncio
    async def test_send_without_system_prompt(self) -> None:
        b = _backend(model="gpt-4o-test")
        b.set_client_for_testing(AsyncMock())
        b.client.chat.completions.create.return_value = _make_response(content="OK")

        await b.send("Hi")

        call_args: Any = b.client.chat.completions.create.call_args
        messages = call_args.kwargs.get("messages") or call_args[1].get("messages")
        # No system message when prompt is empty
        assert messages[0] == {"role": "user", "content": "Hi"}

    @pytest.mark.asyncio
    async def test_send_empty_response(self) -> None:
        """content=None and no tool_calls produces an empty text block."""
        b = _backend(model="gpt-4o-test")
        b.set_client_for_testing(AsyncMock())
        b.client.chat.completions.create.return_value = _make_response(
            content=None, tool_calls=None
        )

        msg = await b.send("Hello")

        assert len(msg.content) == 1
        assert msg.content[0].kind == "text"
        assert msg.content[0].text == ""

    @pytest.mark.asyncio
    async def test_send_passes_model(self) -> None:
        b = _backend(model="gpt-4o-mini")
        b.set_client_for_testing(AsyncMock())
        b.client.chat.completions.create.return_value = _make_response(content="ok")

        await b.send("test")

        call_args: Any = b.client.chat.completions.create.call_args
        assert call_args.kwargs.get("model") == "gpt-4o-mini"

    @pytest.mark.asyncio
    async def test_send_mixed_text_and_tools(self) -> None:
        """Response with both text content and tool calls."""
        b = _backend(model="gpt-4o-test")
        b.set_client_for_testing(AsyncMock())

        tc = _make_tool_call(name="search", arguments='{"q": "test"}')
        b.client.chat.completions.create.return_value = _make_response(
            content="Let me search for that.", tool_calls=[tc]
        )

        msg = await b.send("Find something")

        text_blocks = [bl for bl in msg.content if bl.kind == "text"]
        tool_blocks = [bl for bl in msg.content if bl.kind == "tool_use"]
        assert len(text_blocks) == 1
        assert text_blocks[0].text == "Let me search for that."
        assert len(tool_blocks) == 1
        assert tool_blocks[0].tool_name == "search"


# ===================================================================
# 4. TestOpenAIStream
# ===================================================================


class TestOpenAIStream:
    """Verify stream() yields correct StreamChunk objects."""

    @pytest.mark.asyncio
    async def test_stream_text_chunks(self) -> None:
        b = _backend(model="gpt-4o-test")
        b.set_client_for_testing(AsyncMock())

        c1 = _make_stream_chunk(content="Hello")
        c2 = _make_stream_chunk(content=" world")

        b.client.chat.completions.create.return_value = _aiter(c1, c2)

        chunks: list[StreamChunk] = []
        async for chunk in b.stream("Hi"):
            chunks.append(chunk)

        text_chunks = [c for c in chunks if c.kind == ChunkKind.TEXT_DELTA]
        assert len(text_chunks) == 2
        assert text_chunks[0].text == "Hello"
        assert text_chunks[1].text == " world"

    @pytest.mark.asyncio
    async def test_stream_done_chunk(self) -> None:
        """The last chunk yielded must always be DONE."""
        b = _backend(model="gpt-4o-test")
        b.set_client_for_testing(AsyncMock())

        c1 = _make_stream_chunk(content="hi")
        b.client.chat.completions.create.return_value = _aiter(c1)

        chunks: list[StreamChunk] = []
        async for chunk in b.stream("test"):
            chunks.append(chunk)

        assert chunks[-1].kind == ChunkKind.DONE
        assert chunks[-1].raw is None

    @pytest.mark.asyncio
    async def test_stream_tool_call_chunks(self) -> None:
        b = _backend(model="gpt-4o-test")
        b.set_client_for_testing(AsyncMock())

        tc: Any = MagicMock()
        tc.function.name = "my_tool"
        tc.function.arguments = '{"key": "val"}'

        c1 = _make_stream_chunk(content=None, tool_calls=[tc])
        b.client.chat.completions.create.return_value = _aiter(c1)

        chunks: list[StreamChunk] = []
        async for chunk in b.stream("call tool"):
            chunks.append(chunk)

        starts = [c for c in chunks if c.kind == ChunkKind.TOOL_USE_START]
        deltas = [c for c in chunks if c.kind == ChunkKind.TOOL_USE_DELTA]
        assert len(starts) == 1
        assert starts[0].tool_name == "my_tool"
        assert len(deltas) == 1
        assert deltas[0].tool_input_delta == '{"key": "val"}'

    @pytest.mark.asyncio
    async def test_stream_tool_call_name_only(self) -> None:
        """Tool call chunk with name but no arguments."""
        b = _backend(model="gpt-4o-test")
        b.set_client_for_testing(AsyncMock())

        tc: Any = MagicMock()
        tc.function.name = "search"
        tc.function.arguments = None  # no arguments yet

        c1 = _make_stream_chunk(content=None, tool_calls=[tc])
        b.client.chat.completions.create.return_value = _aiter(c1)

        chunks: list[StreamChunk] = []
        async for chunk in b.stream("search"):
            chunks.append(chunk)

        starts = [c for c in chunks if c.kind == ChunkKind.TOOL_USE_START]
        deltas = [c for c in chunks if c.kind == ChunkKind.TOOL_USE_DELTA]
        assert len(starts) == 1
        assert len(deltas) == 0  # no arguments delta

    @pytest.mark.asyncio
    async def test_stream_empty_choices(self) -> None:
        """Chunks with no choices are skipped."""
        b = _backend(model="gpt-4o-test")
        b.set_client_for_testing(AsyncMock())

        c_empty = _make_stream_chunk(choices=False)
        c_text = _make_stream_chunk(content="hi")

        b.client.chat.completions.create.return_value = _aiter(c_empty, c_text)

        chunks: list[StreamChunk] = []
        async for chunk in b.stream("test"):
            chunks.append(chunk)

        text_chunks = [c for c in chunks if c.kind == ChunkKind.TEXT_DELTA]
        assert len(text_chunks) == 1
        assert text_chunks[0].text == "hi"

    @pytest.mark.asyncio
    async def test_stream_passes_stream_true(self) -> None:
        """Verify stream=True is passed to the SDK."""
        b = _backend(model="gpt-4o-test")
        b.set_client_for_testing(AsyncMock())
        b.client.chat.completions.create.return_value = _aiter(
            _make_stream_chunk(content="x")
        )

        async for _ in b.stream("test"):
            pass

        call_kwargs: Any = b.client.chat.completions.create.call_args.kwargs
        assert call_kwargs.get("stream") is True

    @pytest.mark.asyncio
    async def test_stream_only_done_for_empty_response(self) -> None:
        """Empty stream response should still yield MESSAGE_START + DONE."""
        b = _backend(model="gpt-4o-test")
        b.set_client_for_testing(AsyncMock())
        b.client.chat.completions.create.return_value = _aiter()  # no chunks

        chunks: list[StreamChunk] = []
        async for chunk in b.stream("test"):
            chunks.append(chunk)

        assert len(chunks) == 2
        assert chunks[0].kind == ChunkKind.MESSAGE_START
        assert chunks[1].kind == ChunkKind.DONE
        assert chunks[1].metadata is not None
        assert chunks[1].metadata.model_id == "gpt-4o-test"


# ===================================================================
# 5. TestOpenAISessions
# ===================================================================


class TestOpenAISessions:
    """Verify session create/resume/list/delete and conversation history."""

    @pytest.mark.asyncio
    async def test_create_session(self) -> None:
        b = _backend()
        ref = await b.create_session()

        assert isinstance(ref, SessionRef)
        assert ref.backend == Backend.OPENAI
        assert ref.session_id in b.conversations
        assert b.active_session == ref.session_id
        assert b.conversations[ref.session_id] == []

    @pytest.mark.asyncio
    async def test_resume_session(self) -> None:
        b = _backend()
        ref = await b.create_session()

        b.set_active_session_for_testing(None)
        await b.resume_session(ref)

        assert b.active_session == ref.session_id

    @pytest.mark.asyncio
    async def test_resume_nonexistent_raises(self) -> None:
        b = _backend()
        fake_ref = SessionRef(session_id="nonexistent-id", backend=Backend.OPENAI)

        with pytest.raises(RuntimeError, match="not found"):
            await b.resume_session(fake_ref)

    @pytest.mark.asyncio
    async def test_list_sessions(self) -> None:
        b = _backend()
        ref1 = await b.create_session()
        ref2 = await b.create_session()

        sessions = await b.list_sessions()

        assert len(sessions) == 2
        ids = {s.session_id for s in sessions}
        assert ref1.session_id in ids
        assert ref2.session_id in ids

    @pytest.mark.asyncio
    async def test_delete_session(self) -> None:
        b = _backend()
        ref = await b.create_session()

        await b.delete_session(ref)

        assert ref.session_id not in b.conversations
        sessions = await b.list_sessions()
        assert len(sessions) == 0

    @pytest.mark.asyncio
    async def test_delete_nonexistent_session_noop(self) -> None:
        b = _backend()
        fake_ref = SessionRef(session_id="ghost", backend=Backend.OPENAI)
        await b.delete_session(fake_ref)  # should not raise

    @pytest.mark.asyncio
    async def test_conversation_history_on_send(self) -> None:
        """After send(), conversation history is appended for the active session."""
        b = _backend(model="gpt-4o-test")
        b.set_client_for_testing(AsyncMock())
        b.client.chat.completions.create.return_value = _make_response(
            content="Reply 1"
        )

        ref = await b.create_session()
        await b.send("Hello")

        history = b.conversations[ref.session_id]
        assert len(history) == 2

        def to_dict(m: Any) -> Any:
            return m.to_dict() if hasattr(m, "to_dict") else m

        assert to_dict(history[0]) == {"role": "user", "content": "Hello"}
        assert to_dict(history[1]) == {"role": "assistant", "content": "Reply 1"}

    @pytest.mark.asyncio
    async def test_conversation_history_included_in_messages(self) -> None:
        """Previous conversation history is sent in subsequent requests."""
        b = _backend(model="gpt-4o-test")
        b.set_client_for_testing(AsyncMock())
        b.client.chat.completions.create.return_value = _make_response(content="Reply")

        await b.create_session()
        await b.send("First message")

        # Reset mock to capture next call
        b.client.chat.completions.create.reset_mock()
        b.client.chat.completions.create.return_value = _make_response(
            content="Reply 2"
        )

        await b.send("Second message")

        call_args: Any = b.client.chat.completions.create.call_args
        messages = call_args.kwargs.get("messages") or call_args[1].get("messages")
        # Should include: history user msg, history assistant msg, new user msg
        assert messages[-3] == {"role": "user", "content": "First message"}
        assert messages[-2] == {"role": "assistant", "content": "Reply"}
        assert messages[-1] == {"role": "user", "content": "Second message"}

    @pytest.mark.asyncio
    async def test_conversation_history_on_stream(self) -> None:
        """Stream also persists conversation history."""
        b = _backend(model="gpt-4o-test")
        b.set_client_for_testing(AsyncMock())
        b.client.chat.completions.create.return_value = _aiter(
            _make_stream_chunk(content="Hi"),
            _make_stream_chunk(content=" there"),
        )

        ref = await b.create_session()
        async for _ in b.stream("Hello stream"):
            pass

        history = b.conversations[ref.session_id]
        assert len(history) == 2

        def to_dict(m: Any) -> Any:
            return m.to_dict() if hasattr(m, "to_dict") else m

        assert to_dict(history[0]) == {"role": "user", "content": "Hello stream"}
        assert to_dict(history[1]) == {"role": "assistant", "content": "Hi there"}

    @pytest.mark.asyncio
    async def test_no_history_without_session(self) -> None:
        """Without an active session, no history is persisted."""
        b = _backend(model="gpt-4o-test")
        b.set_client_for_testing(AsyncMock())
        b.client.chat.completions.create.return_value = _make_response(
            content="Ephemeral reply"
        )

        await b.send("No session")

        assert b.conversations == {}


# ===================================================================
# 6. TestOpenAITools
# ===================================================================


class TestOpenAITools:
    """Verify tool registration and _build_create_kwargs formatting."""

    def test_register_tool(self) -> None:
        b = _backend()
        spec = _sample_tool_spec("greet")

        b.register_tool(spec)

        assert len(b.tools) == 1
        assert b.tools[0].name == "greet"
        assert "greet" in b.tool_registry

    def test_register_multiple_tools(self) -> None:
        b = _backend()
        b.register_tool(_sample_tool_spec("tool_a"))
        b.register_tool(_sample_tool_spec("tool_b"))

        assert len(b.tools) == 2
        assert "tool_a" in b.tool_registry
        assert "tool_b" in b.tool_registry

    def test_get_tool_registry(self) -> None:
        b = _backend()
        reg = b.get_tool_registry()
        assert isinstance(reg, ToolRegistry)

    def test_build_create_kwargs_with_tools(self) -> None:
        """Tools should be formatted in OpenAI function calling format."""
        b = _backend()
        spec = ToolSpec(
            name="weather",
            description="Get weather for a city",
            parameters={
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
            handler=_tool_handler,
        )
        b.register_tool(spec)

        result = b.build_create_kwargs({})

        assert "tools" in result
        assert len(result["tools"]) == 1
        tool_def = result["tools"][0]
        assert tool_def["type"] == "function"
        assert tool_def["function"]["name"] == "weather"
        assert tool_def["function"]["description"] == "Get weather for a city"
        assert (
            tool_def["function"]["parameters"]["properties"]["city"]["type"] == "string"
        )

    def test_build_create_kwargs_no_tools(self) -> None:
        """Without tools registered, 'tools' key should not appear."""
        b = _backend()
        result = b.build_create_kwargs({})
        assert "tools" not in result

    def test_build_create_kwargs_filters(self) -> None:
        """Only valid completion params are passed through."""
        b = _backend()
        kwargs = {
            "temperature": 0.7,
            "top_p": 0.9,
            "max_tokens": 100,
            "stop": ["\n"],
            "frequency_penalty": 0.5,
            "presence_penalty": 0.3,
            "seed": 42,
            # Invalid params that should be filtered out
            "invalid_param": "should not appear",
            "custom_thing": True,
            "model": "should-not-pass",
        }

        result = b.build_create_kwargs(kwargs)

        assert result["temperature"] == 0.7
        assert result["top_p"] == 0.9
        assert result["max_tokens"] == 100
        assert result["stop"] == ["\n"]
        assert result["frequency_penalty"] == 0.5
        assert result["presence_penalty"] == 0.3
        assert result["seed"] == 42
        assert "invalid_param" not in result
        assert "custom_thing" not in result
        assert "model" not in result

    def test_build_create_kwargs_response_format(self) -> None:
        """OpenAI supports response_format; it should pass through."""
        b = _backend()
        kwargs = {"response_format": {"type": "json_object"}}

        result = b.build_create_kwargs(kwargs)

        assert result["response_format"] == {"type": "json_object"}

    def test_build_create_kwargs_empty(self) -> None:
        b = _backend()
        result = b.build_create_kwargs({})
        assert result == {}


# ===================================================================
# 7. TestOpenAIHooks
# ===================================================================


class TestOpenAIHooks:
    """Verify hook registration and execution during send/stream."""

    def test_register_hook(self) -> None:
        b = _backend()
        callback: Any = MagicMock()

        b.register_hook(HookPoint.STOP, callback)

        assert callback in b.hooks[HookPoint.STOP]

    def test_register_multiple_hooks(self) -> None:
        b = _backend()
        cb1: Any = MagicMock()
        cb2: Any = MagicMock()

        b.register_hook(HookPoint.STOP, cb1)
        b.register_hook(HookPoint.STOP, cb2)

        assert len(b.hooks[HookPoint.STOP]) == 2

    def test_register_hook_different_points(self) -> None:
        b = _backend()
        cb_prompt: Any = MagicMock()
        cb_stop: Any = MagicMock()

        b.register_hook(HookPoint.USER_PROMPT_SUBMITTED, cb_prompt)
        b.register_hook(HookPoint.STOP, cb_stop)

        assert cb_prompt in b.hooks[HookPoint.USER_PROMPT_SUBMITTED]
        assert cb_stop in b.hooks[HookPoint.STOP]

    @pytest.mark.asyncio
    async def test_hooks_called_on_send(self) -> None:
        b = _backend(model="gpt-4o-test")
        b.set_client_for_testing(AsyncMock())
        b.client.chat.completions.create.return_value = _make_response(content="ok")

        prompt_hook: Any = MagicMock()
        stop_hook: Any = MagicMock()
        b.register_hook(HookPoint.USER_PROMPT_SUBMITTED, prompt_hook)
        b.register_hook(HookPoint.STOP, stop_hook)

        await b.send("Hello")

        prompt_hook.assert_called_once()
        ctx = prompt_hook.call_args[0][0]
        assert isinstance(ctx, HookContext)
        assert ctx.hook == HookPoint.USER_PROMPT_SUBMITTED
        assert ctx.prompt == "Hello"

        stop_hook.assert_called_once()
        stop_ctx = stop_hook.call_args[0][0]
        assert stop_ctx.hook == HookPoint.STOP

    @pytest.mark.asyncio
    async def test_hooks_called_on_stream(self) -> None:
        b = _backend(model="gpt-4o-test")
        b.set_client_for_testing(AsyncMock())
        b.client.chat.completions.create.return_value = _aiter(
            _make_stream_chunk(content="hi")
        )

        prompt_hook: Any = MagicMock()
        stop_hook: Any = MagicMock()
        b.register_hook(HookPoint.USER_PROMPT_SUBMITTED, prompt_hook)
        b.register_hook(HookPoint.STOP, stop_hook)

        async for _ in b.stream("Hello"):
            pass

        prompt_hook.assert_called_once()
        stop_hook.assert_called_once()

    @pytest.mark.asyncio
    async def test_async_hooks_awaited(self) -> None:
        b = _backend(model="gpt-4o-test")
        b.set_client_for_testing(AsyncMock())
        b.client.chat.completions.create.return_value = _make_response(content="ok")

        call_log: list[Any] = []

        async def async_hook(ctx: HookContext):
            call_log.append(ctx.hook)

        b.register_hook(HookPoint.USER_PROMPT_SUBMITTED, async_hook)
        b.register_hook(HookPoint.STOP, async_hook)

        await b.send("test")

        assert HookPoint.USER_PROMPT_SUBMITTED in call_log
        assert HookPoint.STOP in call_log

    @pytest.mark.asyncio
    async def test_hook_error_silenced(self) -> None:
        """A hook that raises an exception should not break send()."""
        b = _backend(model="gpt-4o-test")
        b.set_client_for_testing(AsyncMock())
        b.client.chat.completions.create.return_value = _make_response(
            content="still works"
        )

        def bad_hook(ctx: Any) -> None:
            raise ValueError("hook exploded")

        b.register_hook(HookPoint.USER_PROMPT_SUBMITTED, bad_hook)

        # Should not raise despite the hook error
        msg = await b.send("Hello")
        assert msg.text == "still works"

    @pytest.mark.asyncio
    async def test_async_hook_error_silenced(self) -> None:
        """An async hook that raises is also silenced."""
        b = _backend(model="gpt-4o-test")
        b.set_client_for_testing(AsyncMock())
        b.client.chat.completions.create.return_value = _make_response(content="fine")

        async def bad_async_hook(ctx: Any) -> None:
            raise RuntimeError("async hook exploded")

        b.register_hook(HookPoint.STOP, bad_async_hook)

        msg = await b.send("test")
        assert msg.text == "fine"

    @pytest.mark.asyncio
    async def test_hook_error_silenced_on_stream(self) -> None:
        """Hook errors during stream should not break iteration."""
        b = _backend(model="gpt-4o-test")
        b.set_client_for_testing(AsyncMock())
        b.client.chat.completions.create.return_value = _aiter(
            _make_stream_chunk(content="ok")
        )

        def bad_hook(ctx: Any) -> None:
            raise Exception("boom")

        b.register_hook(HookPoint.USER_PROMPT_SUBMITTED, bad_hook)

        chunks: list[StreamChunk] = []
        async for chunk in b.stream("test"):
            chunks.append(chunk)

        text_chunks = [c for c in chunks if c.kind == ChunkKind.TEXT_DELTA]
        assert len(text_chunks) == 1


# ===================================================================
# 8. TestOpenAIBuildMessages (internal helper)
# ===================================================================


class TestOpenAIBuildMessages:
    """Verify _build_messages() constructs the correct message list."""

    def test_user_only(self) -> None:
        b = _backend()
        messages = b.build_messages("Hello")
        assert messages == [{"role": "user", "content": "Hello"}]

    def test_with_system_prompt(self) -> None:
        b = _backend(system_prompt="You are helpful.")
        messages = b.build_messages("Hi")
        assert messages[0] == {"role": "system", "content": "You are helpful."}
        assert messages[-1] == {"role": "user", "content": "Hi"}

    def test_with_conversation_history(self) -> None:
        b = _backend()
        b.conversations["sess1"] = [
            ChatMessage(role="user", content="First"),
            ChatMessage(role="assistant", content="Response"),
        ]
        b.set_active_session_for_testing("sess1")

        messages = b.build_messages("Second")
        assert len(messages) == 3
        assert messages[0] == {"role": "user", "content": "First"}
        assert messages[1] == {"role": "assistant", "content": "Response"}
        assert messages[2] == {"role": "user", "content": "Second"}

    def test_with_system_and_history(self) -> None:
        b = _backend(system_prompt="System msg")
        b.conversations["sess1"] = [
            ChatMessage(role="user", content="Prev"),
            ChatMessage(role="assistant", content="Reply"),
        ]
        b.set_active_session_for_testing("sess1")

        messages = b.build_messages("New")
        assert messages[0] == {"role": "system", "content": "System msg"}
        assert messages[1] == {"role": "user", "content": "Prev"}
        assert messages[2] == {"role": "assistant", "content": "Reply"}
        assert messages[3] == {"role": "user", "content": "New"}


# ===================================================================
# 9. TestOpenAIToMessage (internal helper)
# ===================================================================


class TestOpenAIToMessage:
    """Verify _to_message() correctly normalizes OpenAI responses."""

    def test_text_only(self) -> None:
        b = _backend()
        resp = _make_response(content="Simple text")
        msg = b.to_message(resp)

        assert msg.role == Role.ASSISTANT
        assert msg.backend == Backend.OPENAI
        assert len(msg.content) == 1
        assert msg.content[0].kind == "text"
        assert msg.content[0].text == "Simple text"

    def test_tool_calls_only(self) -> None:
        b = _backend()
        tc = _make_tool_call(name="fn", arguments='{"a": 1}')
        resp = _make_response(content=None, tool_calls=[tc])
        msg = b.to_message(resp)

        assert len(msg.content) == 1
        assert msg.content[0].kind == "tool_use"
        assert msg.content[0].tool_name == "fn"
        assert msg.content[0].tool_input == {"a": 1}

    def test_multiple_tool_calls(self) -> None:
        b = _backend()
        tc1 = _make_tool_call(name="fn1", arguments='{"x": 1}', tc_id="call_1")
        tc2 = _make_tool_call(name="fn2", arguments='{"y": 2}', tc_id="call_2")
        resp = _make_response(content=None, tool_calls=[tc1, tc2])
        msg = b.to_message(resp)

        assert len(msg.content) == 2
        assert msg.content[0].tool_name == "fn1"
        assert msg.content[1].tool_name == "fn2"

    def test_empty_response_fallback(self) -> None:
        b = _backend()
        resp = _make_response(content=None, tool_calls=None)
        msg = b.to_message(resp)

        assert len(msg.content) == 1
        assert msg.content[0].kind == "text"
        assert msg.content[0].text == ""

    def test_raw_preserved(self) -> None:
        b = _backend()
        resp = _make_response(content="hi")
        msg = b.to_message(resp)
        assert msg.raw is resp
