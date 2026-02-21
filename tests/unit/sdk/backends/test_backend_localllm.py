"""Tests for sdk.backends.localllm — LocalLLMBackend."""

from __future__ import annotations

from typing import Any

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from sdk.internal.auth import AuthConfig
from sdk.internal.types import Backend, ChunkKind, HookPoint, StreamChunk


def _make_auth(**kw: str | None) -> AuthConfig:
    return AuthConfig(localllm_base_url=kw.get("base_url", "http://localhost:1234/v1"))


class TestLocalLLMInit:
    def test_defaults(self) -> None:
        from sdk.backends.localllm import LocalLLMBackend

        b = LocalLLMBackend(_make_auth())
        assert b.base_url == "http://localhost:1234/v1"
        assert b.model is None
        assert b.client is None

    def test_with_model(self) -> None:
        from sdk.backends.localllm import LocalLLMBackend

        b = LocalLLMBackend(_make_auth(), model="llama-3", system_prompt="Be concise")
        assert b.model == "llama-3"
        assert b.system_prompt == "Be concise"

    def test_capabilities_include_native_features(self) -> None:
        from sdk.backends.localllm import LocalLLMBackend

        b = LocalLLMBackend(_make_auth())
        caps = b.capabilities()
        assert caps.supports_native_mode is True
        assert "health_check" in caps.native_features


class TestLocalLLMLifecycle:
    @pytest.mark.asyncio
    async def test_start_discovers_model(self) -> None:
        from sdk.backends.localllm import LocalLLMBackend

        b = LocalLLMBackend(_make_auth())

        mock_client: Any = AsyncMock()
        mock_model = MagicMock()
        mock_model.id = "local-model"
        mock_client.models.list.return_value = MagicMock(data=[mock_model])

        with patch("openai.AsyncOpenAI", return_value=mock_client):
            await b.start()
            assert b.client is mock_client
            assert b.model == "local-model"

    @pytest.mark.asyncio
    async def test_start_with_model_set(self) -> None:
        from sdk.backends.localllm import LocalLLMBackend

        b = LocalLLMBackend(_make_auth(), model="llama-3")

        mock_client: Any = AsyncMock()
        with patch("openai.AsyncOpenAI", return_value=mock_client):
            await b.start()
            assert b.model == "llama-3"
            # Should NOT call models.list
            mock_client.models.list.assert_not_called()

    @pytest.mark.asyncio
    async def test_stop(self) -> None:
        from sdk.backends.localllm import LocalLLMBackend

        b = LocalLLMBackend(_make_auth())
        b.set_client_for_testing(AsyncMock())
        await b.stop()
        assert b.client is None


class TestLocalLLMSend:
    @pytest.mark.asyncio
    async def test_send(self) -> None:
        from sdk.backends.localllm import LocalLLMBackend

        b = LocalLLMBackend(_make_auth(), model="llama-3")
        mock_client: Any = AsyncMock()

        mock_choice = MagicMock()
        mock_choice.message.content = "Hello from local LLM"
        mock_choice.message.tool_calls = None
        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_client.chat.completions.create.return_value = mock_response
        b.set_client_for_testing(mock_client)

        msg = await b.send("Hello")
        assert msg.content[0].text == "Hello from local LLM"

    @pytest.mark.asyncio
    async def test_send_not_started(self) -> None:
        from sdk.backends.localllm import LocalLLMBackend

        b = LocalLLMBackend(_make_auth())
        with pytest.raises(RuntimeError):
            await b.send("test")


class TestLocalLLMStream:
    @pytest.mark.asyncio
    async def test_stream(self) -> None:
        from sdk.backends.localllm import LocalLLMBackend

        b = LocalLLMBackend(_make_auth(), model="llama-3")
        mock_client: Any = AsyncMock()

        # Create mock stream chunks
        chunk1 = MagicMock()
        chunk1.choices = [MagicMock()]
        chunk1.choices[0].delta.content = "Hello"
        chunk1.choices[0].delta.tool_calls = None

        chunk2 = MagicMock()
        chunk2.choices = [MagicMock()]
        chunk2.choices[0].delta.content = " world"
        chunk2.choices[0].delta.tool_calls = None

        async def mock_stream() -> Any:
            yield chunk1
            yield chunk2

        mock_client.chat.completions.create.return_value = mock_stream()
        b.set_client_for_testing(mock_client)

        chunks: list[StreamChunk] = []
        async for c in b.stream("Hi"):
            chunks.append(c)

        text_chunks: list[StreamChunk] = [c for c in chunks if c.kind == ChunkKind.TEXT_DELTA]
        assert len(text_chunks) == 2
        assert text_chunks[0].text == "Hello"
        assert text_chunks[0].native_event is chunk1
        done_chunks: list[StreamChunk] = [c for c in chunks if c.kind == ChunkKind.DONE]
        assert len(done_chunks) == 1


class TestLocalLLMSessions:
    @pytest.mark.asyncio
    async def test_create_session(self) -> None:
        from sdk.backends.localllm import LocalLLMBackend

        b = LocalLLMBackend(_make_auth())
        b.set_client_for_testing(MagicMock())
        ref = await b.create_session()
        assert ref.backend == Backend.LOCALLLM
        assert b.active_session == ref.session_id

    @pytest.mark.asyncio
    async def test_resume_session(self) -> None:
        from sdk.backends.localllm import LocalLLMBackend

        b = LocalLLMBackend(_make_auth())
        b.set_client_for_testing(MagicMock())
        ref = await b.create_session()
        b._active_session = None  # pyright: ignore[reportPrivateUsage]
        await b.resume_session(ref)
        assert b.active_session == ref.session_id

    @pytest.mark.asyncio
    async def test_resume_unknown_session(self) -> None:
        from sdk.backends.localllm import LocalLLMBackend
        from sdk.internal.types import SessionRef

        b = LocalLLMBackend(_make_auth())
        b.set_client_for_testing(MagicMock())
        ref = SessionRef(session_id="unknown", backend=Backend.LOCALLLM)
        with pytest.raises(RuntimeError, match="not found"):
            await b.resume_session(ref)

    @pytest.mark.asyncio
    async def test_delete_session(self) -> None:
        from sdk.backends.localllm import LocalLLMBackend

        b = LocalLLMBackend(_make_auth())
        b.set_client_for_testing(MagicMock())
        ref = await b.create_session()
        await b.delete_session(ref)
        assert ref.session_id not in b.conversations


class TestLocalLLMTools:
    def test_register_tool(self) -> None:
        from sdk.backends.localllm import LocalLLMBackend
        from sdk.internal.types import ToolSpec

        b = LocalLLMBackend(_make_auth())
        spec = ToolSpec(
            name="t1", description="test", parameters={}, handler=lambda: None
        )
        b.register_tool(spec)
        assert len(b.tools) == 1

    def test_register_hook(self) -> None:
        from sdk.backends.localllm import LocalLLMBackend

        b = LocalLLMBackend(_make_auth())
        cb = MagicMock()
        b.register_hook(HookPoint.PRE_TOOL_USE, cb)
        assert cb in b.hooks[HookPoint.PRE_TOOL_USE]
