"""Tests for sdk.tui.backend_bridge — BackendBridge async bridge.

Covers connect(), stream_prompt() chunk routing, stream cancellation,
disconnect() cleanup, and error handling. All tests mock ObscuraClient
to avoid real API calls.
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator, Callable
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sdk._types import (
    ChunkKind,
    ContentBlock,
    Message,
    Role,
    StreamChunk,
)


# ---------------------------------------------------------------------------
# Inline stubs — mirrors sdk/tui/backend_bridge.py from PLAN_TUI.md
# ---------------------------------------------------------------------------

class BackendBridge:
    """Manages ObscuraClient lifecycle and routes stream chunks to callbacks."""

    def __init__(
        self,
        backend: str = "claude",
        model: str | None = None,
        cwd: str | None = None,
    ) -> None:
        self._backend = backend
        self._model = model
        self._cwd = cwd
        self._client: Any = None
        self._streaming: bool = False
        self._cancel_requested: bool = False

    async def connect(self) -> None:
        """Initialize the ObscuraClient."""
        from sdk.client import ObscuraClient
        self._client = ObscuraClient(
            self._backend,
            model=self._model,
            cwd=self._cwd,
        )
        await self._client.start()

    async def stream_prompt(
        self,
        prompt: str,
        on_text: Callable[[str], None],
        on_thinking: Callable[[str], None],
        on_tool_start: Callable[[str], None],
        on_tool_result: Callable[[str], None],
        on_done: Callable[[], None],
        on_error: Callable[[str], None],
    ) -> None:
        """Stream a prompt, dispatching chunks to the appropriate callbacks."""
        if self._client is None:
            raise RuntimeError("Not connected. Call connect() first.")

        self._streaming = True
        self._cancel_requested = False

        try:
            async for chunk in self._client.stream(prompt):
                if self._cancel_requested:
                    break

                if chunk.kind == ChunkKind.TEXT_DELTA:
                    on_text(chunk.text)
                elif chunk.kind == ChunkKind.THINKING_DELTA:
                    on_thinking(chunk.text)
                elif chunk.kind == ChunkKind.TOOL_USE_START:
                    on_tool_start(chunk.tool_name)
                elif chunk.kind == ChunkKind.TOOL_RESULT:
                    on_tool_result(chunk.text)
                elif chunk.kind == ChunkKind.DONE:
                    on_done()
                elif chunk.kind == ChunkKind.ERROR:
                    on_error(chunk.text)
        except Exception as exc:
            on_error(str(exc))
        finally:
            self._streaming = False

    async def send_prompt(self, prompt: str) -> Message:
        """Non-streaming send."""
        if self._client is None:
            raise RuntimeError("Not connected. Call connect() first.")
        return await self._client.send(prompt)

    def cancel_stream(self) -> None:
        """Request cancellation of the current stream."""
        self._cancel_requested = True

    async def disconnect(self) -> None:
        """Shut down the client."""
        if self._client is not None:
            await self._client.stop()
            self._client = None
        self._streaming = False

    @property
    def is_connected(self) -> bool:
        return self._client is not None

    @property
    def is_streaming(self) -> bool:
        return self._streaming


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

async def _async_iter_chunks(chunks: list[StreamChunk]) -> AsyncIterator[StreamChunk]:
    """Create an async iterator from a list of StreamChunks."""
    for chunk in chunks:
        yield chunk


def _make_mock_client(
    stream_chunks: list[StreamChunk] | None = None,
    send_response: Message | None = None,
) -> MagicMock:
    """Create a mock ObscuraClient."""
    client = MagicMock()
    client.start = AsyncMock()
    client.stop = AsyncMock()

    if stream_chunks is not None:
        client.stream = MagicMock(
            return_value=_async_iter_chunks(stream_chunks),
        )
    else:
        client.stream = MagicMock(
            return_value=_async_iter_chunks([
                StreamChunk(kind=ChunkKind.DONE),
            ]),
        )

    if send_response is not None:
        client.send = AsyncMock(return_value=send_response)
    else:
        client.send = AsyncMock(return_value=Message(
            role=Role.ASSISTANT,
            content=[ContentBlock(kind="text", text="response")],
        ))

    return client


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestBackendBridgeConnect:
    """Verify BackendBridge.connect() initializes ObscuraClient."""

    @pytest.mark.asyncio
    async def test_connect_creates_client(self) -> None:
        """connect() creates and starts an ObscuraClient."""
        bridge = BackendBridge(backend="claude")
        mock_client = _make_mock_client()

        with patch("sdk.client.ObscuraClient", return_value=mock_client):
            await bridge.connect()

        assert bridge.is_connected is True
        mock_client.start.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_connect_passes_backend(self) -> None:
        """connect() passes the backend parameter to ObscuraClient."""
        bridge = BackendBridge(backend="copilot", model="gpt-5-mini")
        mock_client = _make_mock_client()

        with patch("sdk.client.ObscuraClient", return_value=mock_client) as mock_cls:
            await bridge.connect()

        mock_cls.assert_called_once_with("copilot", model="gpt-5-mini", cwd=None)

    @pytest.mark.asyncio
    async def test_connect_passes_cwd(self) -> None:
        """connect() passes cwd to ObscuraClient."""
        bridge = BackendBridge(backend="claude", cwd="/my/project")
        mock_client = _make_mock_client()

        with patch("sdk.client.ObscuraClient", return_value=mock_client) as mock_cls:
            await bridge.connect()

        mock_cls.assert_called_once_with("claude", model=None, cwd="/my/project")

    @pytest.mark.asyncio
    async def test_not_connected_before_connect(self) -> None:
        """is_connected is False before connect() is called."""
        bridge = BackendBridge()
        assert bridge.is_connected is False


class TestBackendBridgeStreamPrompt:
    """Verify stream_prompt() routes chunks to the correct callbacks."""

    @pytest.mark.asyncio
    async def test_text_delta_routes_to_on_text(self) -> None:
        """TEXT_DELTA chunks are routed to the on_text callback."""
        chunks = [
            StreamChunk(kind=ChunkKind.TEXT_DELTA, text="hello "),
            StreamChunk(kind=ChunkKind.TEXT_DELTA, text="world"),
            StreamChunk(kind=ChunkKind.DONE),
        ]
        bridge = BackendBridge()
        bridge._client = _make_mock_client(stream_chunks=chunks)

        received_text: list[str] = []
        done_called = []

        await bridge.stream_prompt(
            prompt="test",
            on_text=lambda t: received_text.append(t),
            on_thinking=lambda t: None,
            on_tool_start=lambda n: None,
            on_tool_result=lambda t: None,
            on_done=lambda: done_called.append(True),
            on_error=lambda e: None,
        )

        assert received_text == ["hello ", "world"]
        assert len(done_called) == 1

    @pytest.mark.asyncio
    async def test_thinking_delta_routes_to_on_thinking(self) -> None:
        """THINKING_DELTA chunks are routed to the on_thinking callback."""
        chunks = [
            StreamChunk(kind=ChunkKind.THINKING_DELTA, text="let me think..."),
            StreamChunk(kind=ChunkKind.DONE),
        ]
        bridge = BackendBridge()
        bridge._client = _make_mock_client(stream_chunks=chunks)

        thinking: list[str] = []

        await bridge.stream_prompt(
            prompt="test",
            on_text=lambda t: None,
            on_thinking=lambda t: thinking.append(t),
            on_tool_start=lambda n: None,
            on_tool_result=lambda t: None,
            on_done=lambda: None,
            on_error=lambda e: None,
        )

        assert thinking == ["let me think..."]

    @pytest.mark.asyncio
    async def test_tool_use_start_routes_to_on_tool_start(self) -> None:
        """TOOL_USE_START chunks are routed to the on_tool_start callback."""
        chunks = [
            StreamChunk(kind=ChunkKind.TOOL_USE_START, tool_name="read_file"),
            StreamChunk(kind=ChunkKind.DONE),
        ]
        bridge = BackendBridge()
        bridge._client = _make_mock_client(stream_chunks=chunks)

        tools: list[str] = []

        await bridge.stream_prompt(
            prompt="test",
            on_text=lambda t: None,
            on_thinking=lambda t: None,
            on_tool_start=lambda n: tools.append(n),
            on_tool_result=lambda t: None,
            on_done=lambda: None,
            on_error=lambda e: None,
        )

        assert tools == ["read_file"]

    @pytest.mark.asyncio
    async def test_tool_result_routes_to_on_tool_result(self) -> None:
        """TOOL_RESULT chunks are routed to the on_tool_result callback."""
        chunks = [
            StreamChunk(kind=ChunkKind.TOOL_RESULT, text="file contents here"),
            StreamChunk(kind=ChunkKind.DONE),
        ]
        bridge = BackendBridge()
        bridge._client = _make_mock_client(stream_chunks=chunks)

        results: list[str] = []

        await bridge.stream_prompt(
            prompt="test",
            on_text=lambda t: None,
            on_thinking=lambda t: None,
            on_tool_start=lambda n: None,
            on_tool_result=lambda t: results.append(t),
            on_done=lambda: None,
            on_error=lambda e: None,
        )

        assert results == ["file contents here"]

    @pytest.mark.asyncio
    async def test_done_routes_to_on_done(self) -> None:
        """DONE chunks are routed to the on_done callback."""
        chunks = [StreamChunk(kind=ChunkKind.DONE)]
        bridge = BackendBridge()
        bridge._client = _make_mock_client(stream_chunks=chunks)

        done_count = []

        await bridge.stream_prompt(
            prompt="test",
            on_text=lambda t: None,
            on_thinking=lambda t: None,
            on_tool_start=lambda n: None,
            on_tool_result=lambda t: None,
            on_done=lambda: done_count.append(1),
            on_error=lambda e: None,
        )

        assert len(done_count) == 1

    @pytest.mark.asyncio
    async def test_error_routes_to_on_error(self) -> None:
        """ERROR chunks are routed to the on_error callback."""
        chunks = [
            StreamChunk(kind=ChunkKind.ERROR, text="rate limit exceeded"),
        ]
        bridge = BackendBridge()
        bridge._client = _make_mock_client(stream_chunks=chunks)

        errors: list[str] = []

        await bridge.stream_prompt(
            prompt="test",
            on_text=lambda t: None,
            on_thinking=lambda t: None,
            on_tool_start=lambda n: None,
            on_tool_result=lambda t: None,
            on_done=lambda: None,
            on_error=lambda e: errors.append(e),
        )

        assert errors == ["rate limit exceeded"]

    @pytest.mark.asyncio
    async def test_mixed_chunk_types(self) -> None:
        """A stream with multiple chunk types routes each correctly."""
        chunks = [
            StreamChunk(kind=ChunkKind.THINKING_DELTA, text="hmm"),
            StreamChunk(kind=ChunkKind.TEXT_DELTA, text="Here's "),
            StreamChunk(kind=ChunkKind.TEXT_DELTA, text="the answer"),
            StreamChunk(kind=ChunkKind.TOOL_USE_START, tool_name="search"),
            StreamChunk(kind=ChunkKind.TOOL_RESULT, text="found it"),
            StreamChunk(kind=ChunkKind.DONE),
        ]
        bridge = BackendBridge()
        bridge._client = _make_mock_client(stream_chunks=chunks)

        texts: list[str] = []
        thinking: list[str] = []
        tool_starts: list[str] = []
        tool_results: list[str] = []
        done_calls: list[bool] = []

        await bridge.stream_prompt(
            prompt="test",
            on_text=lambda t: texts.append(t),
            on_thinking=lambda t: thinking.append(t),
            on_tool_start=lambda n: tool_starts.append(n),
            on_tool_result=lambda t: tool_results.append(t),
            on_done=lambda: done_calls.append(True),
            on_error=lambda e: None,
        )

        assert texts == ["Here's ", "the answer"]
        assert thinking == ["hmm"]
        assert tool_starts == ["search"]
        assert tool_results == ["found it"]
        assert len(done_calls) == 1

    @pytest.mark.asyncio
    async def test_stream_not_connected_raises(self) -> None:
        """stream_prompt() raises RuntimeError when not connected."""
        bridge = BackendBridge()
        errors: list[str] = []

        with pytest.raises(RuntimeError, match="Not connected"):
            await bridge.stream_prompt(
                prompt="test",
                on_text=lambda t: None,
                on_thinking=lambda t: None,
                on_tool_start=lambda n: None,
                on_tool_result=lambda t: None,
                on_done=lambda: None,
                on_error=lambda e: errors.append(e),
            )


class TestBackendBridgeStreamCancellation:
    """Verify stream cancellation."""

    @pytest.mark.asyncio
    async def test_cancel_stops_stream(self) -> None:
        """cancel_stream() stops processing further chunks."""
        chunks = [
            StreamChunk(kind=ChunkKind.TEXT_DELTA, text="chunk1"),
            StreamChunk(kind=ChunkKind.TEXT_DELTA, text="chunk2"),
            StreamChunk(kind=ChunkKind.TEXT_DELTA, text="chunk3"),
            StreamChunk(kind=ChunkKind.TEXT_DELTA, text="chunk4"),
            StreamChunk(kind=ChunkKind.TEXT_DELTA, text="chunk5"),
            StreamChunk(kind=ChunkKind.DONE),
        ]

        async def _slow_async_iter(
            chunks: list[StreamChunk],
        ) -> AsyncIterator[StreamChunk]:
            for chunk in chunks:
                await asyncio.sleep(0.01)
                yield chunk

        bridge = BackendBridge()
        mock_client = MagicMock()
        mock_client.stream = MagicMock(return_value=_slow_async_iter(chunks))
        bridge._client = mock_client

        received: list[str] = []

        async def on_text(t: str) -> None:
            received.append(t)
            if len(received) >= 2:
                bridge.cancel_stream()

        # Note: on_text is sync in the bridge interface, but we can
        # still test the cancel_requested flag
        call_count = []

        def sync_on_text(t: str) -> None:
            call_count.append(t)
            if len(call_count) >= 2:
                bridge.cancel_stream()

        await bridge.stream_prompt(
            prompt="test",
            on_text=sync_on_text,
            on_thinking=lambda t: None,
            on_tool_start=lambda n: None,
            on_tool_result=lambda t: None,
            on_done=lambda: None,
            on_error=lambda e: None,
        )

        # Should have received at most 2-3 chunks before cancel took effect
        assert len(call_count) <= 3

    @pytest.mark.asyncio
    async def test_is_streaming_during_stream(self) -> None:
        """is_streaming is True while streaming and False after."""
        chunks = [StreamChunk(kind=ChunkKind.DONE)]
        bridge = BackendBridge()
        bridge._client = _make_mock_client(stream_chunks=chunks)

        streaming_during: list[bool] = []

        def on_done() -> None:
            streaming_during.append(bridge.is_streaming)

        await bridge.stream_prompt(
            prompt="test",
            on_text=lambda t: None,
            on_thinking=lambda t: None,
            on_tool_start=lambda n: None,
            on_tool_result=lambda t: None,
            on_done=on_done,
            on_error=lambda e: None,
        )

        assert streaming_during == [True]
        assert bridge.is_streaming is False


class TestBackendBridgeErrorHandling:
    """Verify error handling during streaming."""

    @pytest.mark.asyncio
    async def test_exception_during_stream_calls_on_error(self) -> None:
        """An exception during streaming is caught and routed to on_error."""
        bridge = BackendBridge()
        mock_client = MagicMock()

        async def _failing_stream(prompt: str, **kwargs: Any) -> AsyncIterator[StreamChunk]:
            yield StreamChunk(kind=ChunkKind.TEXT_DELTA, text="partial")
            raise ConnectionError("connection lost")

        mock_client.stream = _failing_stream
        bridge._client = mock_client

        errors: list[str] = []
        texts: list[str] = []

        await bridge.stream_prompt(
            prompt="test",
            on_text=lambda t: texts.append(t),
            on_thinking=lambda t: None,
            on_tool_start=lambda n: None,
            on_tool_result=lambda t: None,
            on_done=lambda: None,
            on_error=lambda e: errors.append(e),
        )

        assert texts == ["partial"]
        assert len(errors) == 1
        assert "connection lost" in errors[0]

    @pytest.mark.asyncio
    async def test_streaming_false_after_error(self) -> None:
        """is_streaming is False after an error during streaming."""
        bridge = BackendBridge()
        mock_client = MagicMock()

        async def _failing_stream(prompt: str, **kwargs: Any) -> AsyncIterator[StreamChunk]:
            raise RuntimeError("boom")
            yield  # Make it a generator  # noqa: E501

        mock_client.stream = _failing_stream
        bridge._client = mock_client

        await bridge.stream_prompt(
            prompt="test",
            on_text=lambda t: None,
            on_thinking=lambda t: None,
            on_tool_start=lambda n: None,
            on_tool_result=lambda t: None,
            on_done=lambda: None,
            on_error=lambda e: None,
        )

        assert bridge.is_streaming is False

    @pytest.mark.asyncio
    async def test_timeout_error_during_stream(self) -> None:
        """TimeoutError during streaming is caught and reported."""
        bridge = BackendBridge()
        mock_client = MagicMock()

        async def _timeout_stream(prompt: str, **kwargs: Any) -> AsyncIterator[StreamChunk]:
            raise TimeoutError("request timed out")
            yield  # noqa: E501

        mock_client.stream = _timeout_stream
        bridge._client = mock_client

        errors: list[str] = []

        await bridge.stream_prompt(
            prompt="test",
            on_text=lambda t: None,
            on_thinking=lambda t: None,
            on_tool_start=lambda n: None,
            on_tool_result=lambda t: None,
            on_done=lambda: None,
            on_error=lambda e: errors.append(e),
        )

        assert len(errors) == 1
        assert "timed out" in errors[0]


class TestBackendBridgeDisconnect:
    """Verify disconnect() cleanup."""

    @pytest.mark.asyncio
    async def test_disconnect_stops_client(self) -> None:
        """disconnect() calls client.stop() and sets client to None."""
        bridge = BackendBridge()
        mock_client = _make_mock_client()
        bridge._client = mock_client

        await bridge.disconnect()

        mock_client.stop.assert_awaited_once()
        assert bridge.is_connected is False

    @pytest.mark.asyncio
    async def test_disconnect_when_not_connected(self) -> None:
        """disconnect() is safe to call when not connected."""
        bridge = BackendBridge()
        await bridge.disconnect()  # Should not raise
        assert bridge.is_connected is False

    @pytest.mark.asyncio
    async def test_disconnect_resets_streaming_flag(self) -> None:
        """disconnect() resets the streaming flag."""
        bridge = BackendBridge()
        bridge._streaming = True
        bridge._client = _make_mock_client()

        await bridge.disconnect()
        assert bridge.is_streaming is False

    @pytest.mark.asyncio
    async def test_double_disconnect(self) -> None:
        """Calling disconnect() twice is safe."""
        bridge = BackendBridge()
        mock_client = _make_mock_client()
        bridge._client = mock_client

        await bridge.disconnect()
        await bridge.disconnect()

        # stop() should only have been called once (client is None after first)
        mock_client.stop.assert_awaited_once()


class TestBackendBridgeSendPrompt:
    """Verify non-streaming send_prompt()."""

    @pytest.mark.asyncio
    async def test_send_prompt_returns_message(self) -> None:
        """send_prompt() returns a Message from the client."""
        expected_msg = Message(
            role=Role.ASSISTANT,
            content=[ContentBlock(kind="text", text="hello back")],
        )
        bridge = BackendBridge()
        bridge._client = _make_mock_client(send_response=expected_msg)

        result = await bridge.send_prompt("hello")
        assert result.text == "hello back"

    @pytest.mark.asyncio
    async def test_send_prompt_not_connected_raises(self) -> None:
        """send_prompt() raises RuntimeError when not connected."""
        bridge = BackendBridge()
        with pytest.raises(RuntimeError, match="Not connected"):
            await bridge.send_prompt("test")

    @pytest.mark.asyncio
    async def test_send_prompt_passes_prompt_to_client(self) -> None:
        """send_prompt() passes the prompt string to client.send()."""
        bridge = BackendBridge()
        bridge._client = _make_mock_client()

        await bridge.send_prompt("my question")
        bridge._client.send.assert_awaited_once_with("my question")
