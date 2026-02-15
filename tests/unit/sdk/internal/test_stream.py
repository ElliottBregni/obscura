"""Tests for sdk.internal.stream — EventToIteratorBridge and ClaudeIteratorAdapter."""

import pytest
from typing import Any
from unittest.mock import MagicMock

from sdk.internal.stream import EventToIteratorBridge, ClaudeIteratorAdapter
from sdk.internal.types import ChunkKind, StreamChunk


class TestEventToIteratorBridge:
    @pytest.mark.asyncio
    async def test_push_and_iterate(self) -> None:
        bridge = EventToIteratorBridge()
        chunk = StreamChunk(kind=ChunkKind.TEXT_DELTA, text="hello")
        bridge.push(chunk)
        bridge._queue.put_nowait(None)  # pyright: ignore[reportPrivateUsage]

        collected: list[StreamChunk] = []
        async for c in bridge:
            collected.append(c)
        assert len(collected) == 1
        assert collected[0].text == "hello"

    @pytest.mark.asyncio
    async def test_on_text_delta_with_delta_content(self) -> None:
        bridge = EventToIteratorBridge()
        event: Any = MagicMock()
        event.data.delta_content = "hello"
        bridge.on_text_delta(event)
        bridge._queue.put_nowait(None)  # pyright: ignore[reportPrivateUsage]

        chunks: list[StreamChunk] = []
        async for c in bridge:
            chunks.append(c)
        assert len(chunks) == 1
        assert chunks[0].kind == ChunkKind.TEXT_DELTA
        assert chunks[0].text == "hello"

    @pytest.mark.asyncio
    async def test_on_text_delta_with_content(self) -> None:
        bridge = EventToIteratorBridge()
        event: Any = MagicMock()
        event.data.delta_content = None
        event.data.content = "world"
        bridge.on_text_delta(event)
        bridge._queue.put_nowait(None)  # pyright: ignore[reportPrivateUsage]

        chunks: list[StreamChunk] = []
        async for c in bridge:
            chunks.append(c)
        assert chunks[0].text == "world"

    @pytest.mark.asyncio
    async def test_on_text_delta_with_delta(self) -> None:
        bridge = EventToIteratorBridge()
        event: Any = MagicMock()
        event.data.delta_content = None
        event.data.content = None
        event.data.delta = "delta_text"
        bridge.on_text_delta(event)
        bridge._queue.put_nowait(None)  # pyright: ignore[reportPrivateUsage]

        chunks: list[StreamChunk] = []
        async for c in bridge:
            chunks.append(c)
        assert chunks[0].text == "delta_text"

    @pytest.mark.asyncio
    async def test_on_text_delta_string_event(self) -> None:
        bridge = EventToIteratorBridge()
        bridge.on_text_delta("raw string")
        bridge._queue.put_nowait(None)  # pyright: ignore[reportPrivateUsage]

        chunks: list[StreamChunk] = []
        async for c in bridge:
            chunks.append(c)
        assert chunks[0].text == "raw string"

    @pytest.mark.asyncio
    async def test_on_thinking_delta(self) -> None:
        bridge = EventToIteratorBridge()
        event: Any = MagicMock()
        event.data.delta_content = "thinking..."
        bridge.on_thinking_delta(event)
        bridge._queue.put_nowait(None)  # pyright: ignore[reportPrivateUsage]

        chunks: list[StreamChunk] = []
        async for c in bridge:
            chunks.append(c)
        assert chunks[0].kind == ChunkKind.THINKING_DELTA

    @pytest.mark.asyncio
    async def test_on_thinking_delta_reasoning_text(self) -> None:
        bridge = EventToIteratorBridge()
        event: Any = MagicMock()
        event.data.delta_content = None
        event.data.reasoning_text = "reason"
        bridge.on_thinking_delta(event)
        bridge._queue.put_nowait(None)  # pyright: ignore[reportPrivateUsage]

        chunks: list[StreamChunk] = []
        async for c in bridge:
            chunks.append(c)
        assert chunks[0].text == "reason"

    @pytest.mark.asyncio
    async def test_on_thinking_delta_string(self) -> None:
        bridge = EventToIteratorBridge()
        bridge.on_thinking_delta("string thinking")
        bridge._queue.put_nowait(None)  # pyright: ignore[reportPrivateUsage]

        chunks: list[StreamChunk] = []
        async for c in bridge:
            chunks.append(c)
        assert chunks[0].text == "string thinking"

    @pytest.mark.asyncio
    async def test_on_tool_start_with_tool_name(self) -> None:
        bridge = EventToIteratorBridge()
        event: Any = MagicMock()
        event.data.tool_name = "read_file"
        bridge.on_tool_start(event)
        bridge._queue.put_nowait(None)  # pyright: ignore[reportPrivateUsage]

        chunks: list[StreamChunk] = []
        async for c in bridge:
            chunks.append(c)
        assert chunks[0].kind == ChunkKind.TOOL_USE_START
        assert chunks[0].tool_name == "read_file"

    @pytest.mark.asyncio
    async def test_on_tool_start_with_name(self) -> None:
        bridge = EventToIteratorBridge()
        event: Any = MagicMock()
        event.data.tool_name = None
        event.data.name = "write_file"
        bridge.on_tool_start(event)
        bridge._queue.put_nowait(None)  # pyright: ignore[reportPrivateUsage]

        chunks: list[StreamChunk] = []
        async for c in bridge:
            chunks.append(c)
        assert chunks[0].tool_name == "write_file"

    @pytest.mark.asyncio
    async def test_finish(self) -> None:
        bridge = EventToIteratorBridge()
        bridge.finish()

        chunks: list[StreamChunk] = []
        async for c in bridge:
            chunks.append(c)
        assert len(chunks) == 1
        assert chunks[0].kind == ChunkKind.DONE

    @pytest.mark.asyncio
    async def test_error(self) -> None:
        bridge = EventToIteratorBridge()
        bridge.error(RuntimeError("oops"))

        chunks: list[StreamChunk] = []
        async for c in bridge:
            chunks.append(c)
        assert len(chunks) == 1
        assert chunks[0].kind == ChunkKind.ERROR
        assert "oops" in chunks[0].text


class TestClaudeIteratorAdapter:
    @pytest.mark.asyncio
    async def test_result_message(self) -> None:
        result_msg: Any = MagicMock()
        type(result_msg).__name__ = "ResultMessage"

        async def source() -> Any:
            yield result_msg

        adapter = ClaudeIteratorAdapter(source())
        chunks: list[StreamChunk] = []
        async for c in adapter:
            chunks.append(c)
        assert len(chunks) == 1
        assert chunks[0].kind == ChunkKind.DONE

    @pytest.mark.asyncio
    async def test_assistant_message_text_block(self) -> None:
        text_block: Any = MagicMock()
        type(text_block).__name__ = "TextBlock"
        text_block.text = "hello world"

        msg: Any = MagicMock()
        type(msg).__name__ = "AssistantMessage"
        msg.content = [text_block]

        async def source() -> Any:
            yield msg

        adapter = ClaudeIteratorAdapter(source())
        chunks: list[StreamChunk] = []
        async for c in adapter:
            chunks.append(c)
        assert len(chunks) == 1
        assert chunks[0].kind == ChunkKind.TEXT_DELTA
        assert chunks[0].text == "hello world"

    @pytest.mark.asyncio
    async def test_assistant_message_thinking_block(self) -> None:
        thinking_block: Any = MagicMock()
        type(thinking_block).__name__ = "ThinkingBlock"
        thinking_block.thinking = "let me think"

        msg: Any = MagicMock()
        type(msg).__name__ = "AssistantMessage"
        msg.content = [thinking_block]

        async def source() -> Any:
            yield msg

        adapter = ClaudeIteratorAdapter(source())
        chunks: list[StreamChunk] = []
        async for c in adapter:
            chunks.append(c)
        assert chunks[0].kind == ChunkKind.THINKING_DELTA
        assert chunks[0].text == "let me think"

    @pytest.mark.asyncio
    async def test_assistant_message_tool_use_block(self) -> None:
        tool_block: Any = MagicMock()
        type(tool_block).__name__ = "ToolUseBlock"
        tool_block.name = "read_file"

        msg: Any = MagicMock()
        type(msg).__name__ = "AssistantMessage"
        msg.content = [tool_block]

        async def source() -> Any:
            yield msg

        adapter = ClaudeIteratorAdapter(source())
        chunks: list[StreamChunk] = []
        async for c in adapter:
            chunks.append(c)
        assert chunks[0].kind == ChunkKind.TOOL_USE_START
        assert chunks[0].tool_name == "read_file"

    @pytest.mark.asyncio
    async def test_assistant_message_tool_result_block(self) -> None:
        result_block: Any = MagicMock()
        type(result_block).__name__ = "ToolResultBlock"
        result_block.content = "result text"

        msg: Any = MagicMock()
        type(msg).__name__ = "AssistantMessage"
        msg.content = [result_block]

        async def source() -> Any:
            yield msg

        adapter = ClaudeIteratorAdapter(source())
        chunks: list[StreamChunk] = []
        async for c in adapter:
            chunks.append(c)
        assert chunks[0].kind == ChunkKind.TOOL_RESULT
        assert chunks[0].text == "result text"

    @pytest.mark.asyncio
    async def test_system_message_skipped(self) -> None:
        sys_msg: Any = MagicMock()
        type(sys_msg).__name__ = "SystemMessage"

        result_msg: Any = MagicMock()
        type(result_msg).__name__ = "ResultMessage"

        async def source() -> Any:
            yield sys_msg
            yield result_msg

        adapter = ClaudeIteratorAdapter(source())
        chunks: list[StreamChunk] = []
        async for c in adapter:
            chunks.append(c)
        assert len(chunks) == 1
        assert chunks[0].kind == ChunkKind.DONE

    @pytest.mark.asyncio
    async def test_user_message_skipped(self) -> None:
        user_msg: Any = MagicMock()
        type(user_msg).__name__ = "UserMessage"

        result_msg: Any = MagicMock()
        type(result_msg).__name__ = "ResultMessage"

        async def source() -> Any:
            yield user_msg
            yield result_msg

        adapter = ClaudeIteratorAdapter(source())
        chunks: list[StreamChunk] = []
        async for c in adapter:
            chunks.append(c)
        assert len(chunks) == 1
        assert chunks[0].kind == ChunkKind.DONE

    @pytest.mark.asyncio
    async def test_unknown_type_fallback(self) -> None:
        unknown = "just a string"

        async def source() -> Any:
            yield unknown

        adapter = ClaudeIteratorAdapter(source())
        chunks: list[StreamChunk] = []
        async for c in adapter:
            chunks.append(c)
        assert chunks[0].kind == ChunkKind.TEXT_DELTA

    @pytest.mark.asyncio
    async def test_stream_event_text_delta(self) -> None:
        event: Any = MagicMock()
        type(event).__name__ = "StreamEvent"
        event.event = {
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": "streamed"},
        }

        async def source() -> Any:
            yield event

        adapter = ClaudeIteratorAdapter(source())
        chunks: list[StreamChunk] = []
        async for c in adapter:
            chunks.append(c)
        assert chunks[0].kind == ChunkKind.TEXT_DELTA
        assert chunks[0].text == "streamed"

    @pytest.mark.asyncio
    async def test_stream_event_thinking_delta(self) -> None:
        event: Any = MagicMock()
        type(event).__name__ = "StreamEvent"
        event.event = {
            "type": "content_block_delta",
            "delta": {"type": "thinking_delta", "thinking": "hmm"},
        }

        async def source() -> Any:
            yield event

        adapter = ClaudeIteratorAdapter(source())
        chunks: list[StreamChunk] = []
        async for c in adapter:
            chunks.append(c)
        assert chunks[0].kind == ChunkKind.THINKING_DELTA

    @pytest.mark.asyncio
    async def test_stream_event_input_json_delta(self) -> None:
        event: Any = MagicMock()
        type(event).__name__ = "StreamEvent"
        event.event = {
            "type": "content_block_delta",
            "delta": {"type": "input_json_delta", "partial_json": '{"key":'},
        }

        async def source() -> Any:
            yield event

        adapter = ClaudeIteratorAdapter(source())
        chunks: list[StreamChunk] = []
        async for c in adapter:
            chunks.append(c)
        assert chunks[0].kind == ChunkKind.TOOL_USE_DELTA

    @pytest.mark.asyncio
    async def test_stream_event_content_block_start_tool_use(self) -> None:
        event: Any = MagicMock()
        type(event).__name__ = "StreamEvent"
        event.event = {
            "type": "content_block_start",
            "content_block": {"type": "tool_use", "name": "bash"},
        }

        async def source() -> Any:
            yield event

        adapter = ClaudeIteratorAdapter(source())
        chunks: list[StreamChunk] = []
        async for c in adapter:
            chunks.append(c)
        assert chunks[0].kind == ChunkKind.TOOL_USE_START
        assert chunks[0].tool_name == "bash"

    @pytest.mark.asyncio
    async def test_stream_event_no_event_attr(self) -> None:
        event: Any = MagicMock(spec=[])
        type(event).__name__ = "StreamEvent"

        result_msg: Any = MagicMock()
        type(result_msg).__name__ = "ResultMessage"

        async def source() -> Any:
            yield event
            yield result_msg

        adapter = ClaudeIteratorAdapter(source())
        chunks: list[StreamChunk] = []
        async for c in adapter:
            chunks.append(c)
        assert len(chunks) == 1
        assert chunks[0].kind == ChunkKind.DONE

    @pytest.mark.asyncio
    async def test_multiple_content_blocks(self) -> None:
        text_block: Any = MagicMock()
        type(text_block).__name__ = "TextBlock"
        text_block.text = "part1"

        tool_block: Any = MagicMock()
        type(tool_block).__name__ = "ToolUseBlock"
        tool_block.name = "tool1"

        msg: Any = MagicMock()
        type(msg).__name__ = "AssistantMessage"
        msg.content = [text_block, tool_block]

        async def source() -> Any:
            yield msg

        adapter = ClaudeIteratorAdapter(source())
        chunks: list[StreamChunk] = []
        async for c in adapter:
            chunks.append(c)
        assert len(chunks) == 2
        assert chunks[0].kind == ChunkKind.TEXT_DELTA
        assert chunks[1].kind == ChunkKind.TOOL_USE_START

    @pytest.mark.asyncio
    async def test_assistant_message_no_content(self) -> None:
        msg: Any = MagicMock(spec=[])  # no .content
        type(msg).__name__ = "AssistantMessage"

        result_msg: Any = MagicMock()
        type(result_msg).__name__ = "ResultMessage"

        async def source() -> Any:
            yield msg
            yield result_msg

        adapter = ClaudeIteratorAdapter(source())
        chunks: list[StreamChunk] = []
        async for c in adapter:
            chunks.append(c)
        assert len(chunks) == 1
        assert chunks[0].kind == ChunkKind.DONE
