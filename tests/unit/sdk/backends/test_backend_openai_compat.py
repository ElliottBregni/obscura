"""Tests for sdk.backends.openai_compat — OpenAIBackend."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from sdk.internal.auth import AuthConfig
from sdk.internal.types import Backend, ChunkKind, HookPoint


def _make_auth(**kw):
    return AuthConfig(
        openai_api_key=kw.get("api_key", "sk-test"),
        openai_base_url=kw.get("base_url"),
    )


class TestOpenAIInit:
    def test_defaults(self):
        from sdk.backends.openai_compat import OpenAIBackend
        b = OpenAIBackend(_make_auth())
        assert b._model == "gpt-4o"
        assert b._api_key == "sk-test"
        assert b._base_url is None

    def test_custom_base_url(self):
        from sdk.backends.openai_compat import OpenAIBackend
        b = OpenAIBackend(_make_auth(base_url="https://openrouter.ai/api/v1"))
        assert b._base_url == "https://openrouter.ai/api/v1"

    def test_custom_model(self):
        from sdk.backends.openai_compat import OpenAIBackend
        b = OpenAIBackend(_make_auth(), model="gpt-4-turbo", system_prompt="test")
        assert b._model == "gpt-4-turbo"
        assert b._system_prompt == "test"


class TestOpenAILifecycle:
    @pytest.mark.asyncio
    async def test_start(self):
        from sdk.backends.openai_compat import OpenAIBackend
        b = OpenAIBackend(_make_auth())
        mock_client = AsyncMock()
        with patch("openai.AsyncOpenAI", return_value=mock_client):
            await b.start()
            assert b._client is mock_client

    @pytest.mark.asyncio
    async def test_start_with_base_url(self):
        from sdk.backends.openai_compat import OpenAIBackend
        b = OpenAIBackend(_make_auth(base_url="https://api.together.xyz/v1"))
        mock_client = AsyncMock()
        with patch("openai.AsyncOpenAI", return_value=mock_client) as MockOpenAI:
            await b.start()
            MockOpenAI.assert_called_once_with(
                api_key="sk-test",
                base_url="https://api.together.xyz/v1",
            )

    @pytest.mark.asyncio
    async def test_stop(self):
        from sdk.backends.openai_compat import OpenAIBackend
        b = OpenAIBackend(_make_auth())
        b._client = AsyncMock()
        await b.stop()
        assert b._client is None


class TestOpenAISend:
    @pytest.mark.asyncio
    async def test_send(self):
        from sdk.backends.openai_compat import OpenAIBackend
        b = OpenAIBackend(_make_auth())
        mock_client = AsyncMock()

        mock_choice = MagicMock()
        mock_choice.message.content = "Response from OpenAI"
        mock_choice.message.tool_calls = None
        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_client.chat.completions.create.return_value = mock_response
        b._client = mock_client

        msg = await b.send("Hello")
        assert msg.content[0].text == "Response from OpenAI"

    @pytest.mark.asyncio
    async def test_send_not_started(self):
        from sdk.backends.openai_compat import OpenAIBackend
        b = OpenAIBackend(_make_auth())
        with pytest.raises(RuntimeError):
            await b.send("test")


class TestOpenAIStream:
    @pytest.mark.asyncio
    async def test_stream_text(self):
        from sdk.backends.openai_compat import OpenAIBackend
        b = OpenAIBackend(_make_auth())
        mock_client = AsyncMock()

        chunk1 = MagicMock()
        chunk1.choices = [MagicMock()]
        chunk1.choices[0].delta.content = "Hi"
        chunk1.choices[0].delta.tool_calls = None

        async def mock_stream():
            yield chunk1

        mock_client.chat.completions.create.return_value = mock_stream()
        b._client = mock_client

        chunks = []
        async for c in b.stream("Hello"):
            chunks.append(c)

        text_chunks = [c for c in chunks if c.kind == ChunkKind.TEXT_DELTA]
        assert len(text_chunks) == 1
        assert text_chunks[0].text == "Hi"

    @pytest.mark.asyncio
    async def test_stream_tool_calls(self):
        from sdk.backends.openai_compat import OpenAIBackend
        b = OpenAIBackend(_make_auth())
        mock_client = AsyncMock()

        chunk = MagicMock()
        chunk.choices = [MagicMock()]
        chunk.choices[0].delta.content = None
        tc = MagicMock()
        tc.function.name = "my_tool"
        tc.function.arguments = '{"arg": 1}'
        chunk.choices[0].delta.tool_calls = [tc]

        async def mock_stream():
            yield chunk

        mock_client.chat.completions.create.return_value = mock_stream()
        b._client = mock_client

        chunks = []
        async for c in b.stream("call tool"):
            chunks.append(c)

        tool_starts = [c for c in chunks if c.kind == ChunkKind.TOOL_USE_START]
        assert len(tool_starts) == 1
        assert tool_starts[0].tool_name == "my_tool"


class TestOpenAISessions:
    @pytest.mark.asyncio
    async def test_create_and_resume(self):
        from sdk.backends.openai_compat import OpenAIBackend
        b = OpenAIBackend(_make_auth())
        b._client = MagicMock()

        ref = await b.create_session()
        assert ref.backend == Backend.OPENAI

        b._active_session = None
        await b.resume_session(ref)
        assert b._active_session == ref.session_id

    @pytest.mark.asyncio
    async def test_list_sessions(self):
        from sdk.backends.openai_compat import OpenAIBackend
        b = OpenAIBackend(_make_auth())
        b._client = MagicMock()
        await b.create_session()
        sessions = await b.list_sessions()
        assert len(sessions) == 1

    @pytest.mark.asyncio
    async def test_delete_session(self):
        from sdk.backends.openai_compat import OpenAIBackend
        b = OpenAIBackend(_make_auth())
        b._client = MagicMock()
        ref = await b.create_session()
        await b.delete_session(ref)
        assert ref.session_id not in b._conversations


class TestOpenAITools:
    def test_register_tool(self):
        from sdk.backends.openai_compat import OpenAIBackend
        from sdk.internal.types import ToolSpec
        b = OpenAIBackend(_make_auth())
        spec = ToolSpec(name="tool1", description="desc", parameters={"type": "object"}, handler=lambda: None)
        b.register_tool(spec)
        assert len(b._tools) == 1

    def test_register_hook(self):
        from sdk.backends.openai_compat import OpenAIBackend
        b = OpenAIBackend(_make_auth())
        cb = MagicMock()
        b.register_hook(HookPoint.STOP, cb)
        assert cb in b._hooks[HookPoint.STOP]
