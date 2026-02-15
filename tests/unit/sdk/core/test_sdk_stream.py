"""Tests for sdk.internal.stream — EventToIteratorBridge and ClaudeIteratorAdapter."""

from __future__ import annotations

import asyncio

import pytest

from sdk.internal.stream import ClaudeIteratorAdapter, EventToIteratorBridge
from sdk.internal.types import ChunkKind, StreamChunk


# ---------------------------------------------------------------------------
# EventToIteratorBridge (Copilot adapter)
# ---------------------------------------------------------------------------


class TestEventToIteratorBridge:
    @pytest.mark.asyncio
    async def test_push_and_iterate(self) -> None:
        bridge = EventToIteratorBridge()

        # Push some chunks then finish
        bridge.push(StreamChunk(kind=ChunkKind.TEXT_DELTA, text="hello "))
        bridge.push(StreamChunk(kind=ChunkKind.TEXT_DELTA, text="world"))
        bridge.finish()

        chunks: list[StreamChunk] = []
        async for chunk in bridge:
            chunks.append(chunk)

        texts = [c.text for c in chunks if c.kind == ChunkKind.TEXT_DELTA]
        assert texts == ["hello ", "world"]
        # Last chunk should be DONE
        assert chunks[-1].kind == ChunkKind.DONE

    @pytest.mark.asyncio
    async def test_error_stops_iteration(self) -> None:
        bridge = EventToIteratorBridge()

        bridge.push(StreamChunk(kind=ChunkKind.TEXT_DELTA, text="partial"))
        bridge.error(RuntimeError("boom"))

        chunks: list[StreamChunk] = []
        async for chunk in bridge:
            chunks.append(chunk)

        assert any(c.kind == ChunkKind.ERROR for c in chunks)
        assert any("boom" in c.text for c in chunks if c.kind == ChunkKind.ERROR)

    @pytest.mark.asyncio
    async def test_on_text_delta_with_string(self) -> None:
        bridge = EventToIteratorBridge()
        bridge.on_text_delta("hello")
        bridge.finish()

        chunks: list[StreamChunk] = []
        async for chunk in bridge:
            chunks.append(chunk)

        assert chunks[0].kind == ChunkKind.TEXT_DELTA
        assert chunks[0].text == "hello"

    @pytest.mark.asyncio
    async def test_concurrent_push_and_read(self) -> None:
        """Push from a separate task while iterating."""
        bridge = EventToIteratorBridge()

        async def producer() -> None:
            for i in range(5):
                bridge.push(StreamChunk(kind=ChunkKind.TEXT_DELTA, text=str(i)))
                await asyncio.sleep(0.01)
            bridge.finish()

        task = asyncio.create_task(producer())

        chunks: list[StreamChunk] = []
        async for chunk in bridge:
            chunks.append(chunk)

        await task

        text_chunks = [c for c in chunks if c.kind == ChunkKind.TEXT_DELTA]
        assert len(text_chunks) == 5
        assert [c.text for c in text_chunks] == ["0", "1", "2", "3", "4"]


# ---------------------------------------------------------------------------
# ClaudeIteratorAdapter
# ---------------------------------------------------------------------------


class _FakeTextBlock:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeThinkingBlock:
    def __init__(self, thinking: str) -> None:
        self.thinking = thinking


class _FakeToolUseBlock:
    def __init__(self, name: str) -> None:
        self.name = name
        self.id = "tool-123"
        self.input = {"key": "value"}


class _FakeAssistantMessage:
    def __init__(self, content: list) -> None:
        self.content = content


class _FakeResultMessage:
    session_id = "sess-abc"


class _FakeSystemMessage:
    subtype = "info"


# Rename to match type checking
_FakeTextBlock.__name__ = "TextBlock"
_FakeThinkingBlock.__name__ = "ThinkingBlock"
_FakeToolUseBlock.__name__ = "ToolUseBlock"
_FakeAssistantMessage.__name__ = "AssistantMessage"
_FakeResultMessage.__name__ = "ResultMessage"
_FakeSystemMessage.__name__ = "SystemMessage"


async def _async_iter(items: list) -> None:
    """Helper: create an async iterator from a list."""
    for item in items:
        yield item  # type: ignore[misc]


class TestClaudeIteratorAdapter:
    @pytest.mark.asyncio
    async def test_assistant_text(self) -> None:
        msg = _FakeAssistantMessage([_FakeTextBlock("hello world")])
        adapter = ClaudeIteratorAdapter(_async_iter([msg]))

        chunks: list[StreamChunk] = []
        async for chunk in adapter:
            chunks.append(chunk)

        assert len(chunks) == 1
        assert chunks[0].kind == ChunkKind.TEXT_DELTA
        assert chunks[0].text == "hello world"

    @pytest.mark.asyncio
    async def test_assistant_thinking(self) -> None:
        msg = _FakeAssistantMessage([_FakeThinkingBlock("let me think...")])
        adapter = ClaudeIteratorAdapter(_async_iter([msg]))

        chunks: list[StreamChunk] = []
        async for chunk in adapter:
            chunks.append(chunk)

        assert chunks[0].kind == ChunkKind.THINKING_DELTA
        assert chunks[0].text == "let me think..."

    @pytest.mark.asyncio
    async def test_tool_use(self) -> None:
        msg = _FakeAssistantMessage([_FakeToolUseBlock("read_file")])
        adapter = ClaudeIteratorAdapter(_async_iter([msg]))

        chunks: list[StreamChunk] = []
        async for chunk in adapter:
            chunks.append(chunk)

        assert chunks[0].kind == ChunkKind.TOOL_USE_START
        assert chunks[0].tool_name == "read_file"

    @pytest.mark.asyncio
    async def test_result_message_is_done(self) -> None:
        adapter = ClaudeIteratorAdapter(_async_iter([_FakeResultMessage()]))

        chunks: list[StreamChunk] = []
        async for chunk in adapter:
            chunks.append(chunk)

        assert chunks[0].kind == ChunkKind.DONE

    @pytest.mark.asyncio
    async def test_system_message_skipped(self) -> None:
        messages = [_FakeSystemMessage(), _FakeAssistantMessage([_FakeTextBlock("hi")])]
        adapter = ClaudeIteratorAdapter(_async_iter(messages))

        chunks: list[StreamChunk] = []
        async for chunk in adapter:
            chunks.append(chunk)

        # SystemMessage should be skipped, only the text chunk remains
        assert len(chunks) == 1
        assert chunks[0].text == "hi"

    @pytest.mark.asyncio
    async def test_multiple_content_blocks(self) -> None:
        msg = _FakeAssistantMessage(
            [
                _FakeThinkingBlock("hmm"),
                _FakeTextBlock("here's the answer"),
                _FakeToolUseBlock("search"),
            ]
        )
        adapter = ClaudeIteratorAdapter(_async_iter([msg]))

        chunks: list[StreamChunk] = []
        async for chunk in adapter:
            chunks.append(chunk)

        assert len(chunks) == 3
        assert chunks[0].kind == ChunkKind.THINKING_DELTA
        assert chunks[1].kind == ChunkKind.TEXT_DELTA
        assert chunks[2].kind == ChunkKind.TOOL_USE_START
