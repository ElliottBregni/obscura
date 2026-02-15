"""Tests for sdk._stream — EventToIteratorBridge and ClaudeIteratorAdapter."""
import pytest
import asyncio
from unittest.mock import MagicMock

from sdk._stream import EventToIteratorBridge, ClaudeIteratorAdapter
from sdk._types import ChunkKind, StreamChunk


class TestEventToIteratorBridge:
    @pytest.mark.asyncio
    async def test_push_and_iterate(self):
        bridge = EventToIteratorBridge()
        chunk = StreamChunk(kind=ChunkKind.TEXT_DELTA, text="hello")
        bridge.push(chunk)
        bridge._queue.put_nowait(None)  # Signal end

        collected = []
        async for c in bridge:
            collected.append(c)
        assert len(collected) == 1
        assert collected[0].text == "hello"

    @pytest.mark.asyncio
    async def test_on_text_delta_with_delta_content(self):
        bridge = EventToIteratorBridge()
        event = MagicMock()
        event.data.delta_content = "hello"
        bridge.on_text_delta(event)
        bridge._queue.put_nowait(None)

        chunks = []
        async for c in bridge:
            chunks.append(c)
        assert len(chunks) == 1
        assert chunks[0].kind == ChunkKind.TEXT_DELTA
        assert chunks[0].text == "hello"

    @pytest.mark.asyncio
    async def test_on_text_delta_with_content(self):
        bridge = EventToIteratorBridge()
        event = MagicMock()
        event.data.delta_content = None
        event.data.content = "world"
        bridge.on_text_delta(event)
        bridge._queue.put_nowait(None)

        chunks = []
        async for c in bridge:
            chunks.append(c)
        assert chunks[0].text == "world"

    @pytest.mark.asyncio
    async def test_on_text_delta_with_delta(self):
        bridge = EventToIteratorBridge()
        event = MagicMock()
        event.data.delta_content = None
        event.data.content = None
        event.data.delta = "delta_text"
        bridge.on_text_delta(event)
        bridge._queue.put_nowait(None)

        chunks = []
        async for c in bridge:
            chunks.append(c)
        assert chunks[0].text == "delta_text"

    @pytest.mark.asyncio
    async def test_on_text_delta_string_event(self):
        bridge = EventToIteratorBridge()
        bridge.on_text_delta("raw string")
        bridge._queue.put_nowait(None)

        chunks = []
        async for c in bridge:
            chunks.append(c)
        assert chunks[0].text == "raw string"

    @pytest.mark.asyncio
    async def test_on_thinking_delta(self):
        bridge = EventToIteratorBridge()
        event = MagicMock()
        event.data.delta_content = "thinking..."
        bridge.on_thinking_delta(event)
        bridge._queue.put_nowait(None)

        chunks = []
        async for c in bridge:
            chunks.append(c)
        assert chunks[0].kind == ChunkKind.THINKING_DELTA

    @pytest.mark.asyncio
    async def test_on_thinking_delta_reasoning_text(self):
        bridge = EventToIteratorBridge()
        event = MagicMock()
        event.data.delta_content = None
        event.data.reasoning_text = "reason"
        bridge.on_thinking_delta(event)
        bridge._queue.put_nowait(None)

        chunks = []
        async for c in bridge:
            chunks.append(c)
        assert chunks[0].text == "reason"

    @pytest.mark.asyncio
    async def test_on_thinking_delta_string(self):
        bridge = EventToIteratorBridge()
        bridge.on_thinking_delta("string thinking")
        bridge._queue.put_nowait(None)

        chunks = []
        async for c in bridge:
            chunks.append(c)
        assert chunks[0].text == "string thinking"

    @pytest.mark.asyncio
    async def test_on_tool_start_with_tool_name(self):
        bridge = EventToIteratorBridge()
        event = MagicMock()
        event.data.tool_name = "read_file"
        bridge.on_tool_start(event)
        bridge._queue.put_nowait(None)

        chunks = []
        async for c in bridge:
            chunks.append(c)
        assert chunks[0].kind == ChunkKind.TOOL_USE_START
        assert chunks[0].tool_name == "read_file"

    @pytest.mark.asyncio
    async def test_on_tool_start_with_name(self):
        bridge = EventToIteratorBridge()
        event = MagicMock()
        event.data.tool_name = None
        event.data.name = "write_file"
        bridge.on_tool_start(event)
        bridge._queue.put_nowait(None)

        chunks = []
        async for c in bridge:
            chunks.append(c)
        assert chunks[0].tool_name == "write_file"

    @pytest.mark.asyncio
    async def test_finish(self):
        bridge = EventToIteratorBridge()
        bridge.finish()

        chunks = []
        async for c in bridge:
            chunks.append(c)
        assert len(chunks) == 1
        assert chunks[0].kind == ChunkKind.DONE

    @pytest.mark.asyncio
    async def test_error(self):
        bridge = EventToIteratorBridge()
        bridge.error(RuntimeError("oops"))

        chunks = []
        async for c in bridge:
            chunks.append(c)
        assert len(chunks) == 1
        assert chunks[0].kind == ChunkKind.ERROR
        assert "oops" in chunks[0].text


class TestClaudeIteratorAdapter:
    @pytest.mark.asyncio
    async def test_result_message(self):
        result_msg = MagicMock()
        type(result_msg).__name__ = "ResultMessage"

        async def source():
            yield result_msg

        adapter = ClaudeIteratorAdapter(source())
        chunks = []
        async for c in adapter:
            chunks.append(c)
        assert len(chunks) == 1
        assert chunks[0].kind == ChunkKind.DONE

    @pytest.mark.asyncio
    async def test_assistant_message_text_block(self):
        text_block = MagicMock()
        type(text_block).__name__ = "TextBlock"
        text_block.text = "hello world"

        msg = MagicMock()
        type(msg).__name__ = "AssistantMessage"
        msg.content = [text_block]

        async def source():
            yield msg

        adapter = ClaudeIteratorAdapter(source())
        chunks = []
        async for c in adapter:
            chunks.append(c)
        assert len(chunks) == 1
        assert chunks[0].kind == ChunkKind.TEXT_DELTA
        assert chunks[0].text == "hello world"

    @pytest.mark.asyncio
    async def test_assistant_message_thinking_block(self):
        thinking_block = MagicMock()
        type(thinking_block).__name__ = "ThinkingBlock"
        thinking_block.thinking = "let me think"

        msg = MagicMock()
        type(msg).__name__ = "AssistantMessage"
        msg.content = [thinking_block]

        async def source():
            yield msg

        adapter = ClaudeIteratorAdapter(source())
        chunks = []
        async for c in adapter:
            chunks.append(c)
        assert chunks[0].kind == ChunkKind.THINKING_DELTA
        assert chunks[0].text == "let me think"

    @pytest.mark.asyncio
    async def test_assistant_message_tool_use_block(self):
        tool_block = MagicMock()
        type(tool_block).__name__ = "ToolUseBlock"
        tool_block.name = "read_file"

        msg = MagicMock()
        type(msg).__name__ = "AssistantMessage"
        msg.content = [tool_block]

        async def source():
            yield msg

        adapter = ClaudeIteratorAdapter(source())
        chunks = []
        async for c in adapter:
            chunks.append(c)
        assert chunks[0].kind == ChunkKind.TOOL_USE_START
        assert chunks[0].tool_name == "read_file"

    @pytest.mark.asyncio
    async def test_assistant_message_tool_result_block(self):
        result_block = MagicMock()
        type(result_block).__name__ = "ToolResultBlock"
        result_block.content = "result text"

        msg = MagicMock()
        type(msg).__name__ = "AssistantMessage"
        msg.content = [result_block]

        async def source():
            yield msg

        adapter = ClaudeIteratorAdapter(source())
        chunks = []
        async for c in adapter:
            chunks.append(c)
        assert chunks[0].kind == ChunkKind.TOOL_RESULT
        assert chunks[0].text == "result text"

    @pytest.mark.asyncio
    async def test_system_message_skipped(self):
        sys_msg = MagicMock()
        type(sys_msg).__name__ = "SystemMessage"

        result_msg = MagicMock()
        type(result_msg).__name__ = "ResultMessage"

        async def source():
            yield sys_msg
            yield result_msg

        adapter = ClaudeIteratorAdapter(source())
        chunks = []
        async for c in adapter:
            chunks.append(c)
        assert len(chunks) == 1
        assert chunks[0].kind == ChunkKind.DONE

    @pytest.mark.asyncio
    async def test_user_message_skipped(self):
        user_msg = MagicMock()
        type(user_msg).__name__ = "UserMessage"

        result_msg = MagicMock()
        type(result_msg).__name__ = "ResultMessage"

        async def source():
            yield user_msg
            yield result_msg

        adapter = ClaudeIteratorAdapter(source())
        chunks = []
        async for c in adapter:
            chunks.append(c)
        assert len(chunks) == 1
        assert chunks[0].kind == ChunkKind.DONE

    @pytest.mark.asyncio
    async def test_unknown_type_fallback(self):
        unknown = "just a string"

        async def source():
            yield unknown

        adapter = ClaudeIteratorAdapter(source())
        chunks = []
        async for c in adapter:
            chunks.append(c)
        assert chunks[0].kind == ChunkKind.TEXT_DELTA

    @pytest.mark.asyncio
    async def test_stream_event_text_delta(self):
        event = MagicMock()
        type(event).__name__ = "StreamEvent"
        event.event = {
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": "streamed"},
        }

        async def source():
            yield event

        adapter = ClaudeIteratorAdapter(source())
        chunks = []
        async for c in adapter:
            chunks.append(c)
        assert chunks[0].kind == ChunkKind.TEXT_DELTA
        assert chunks[0].text == "streamed"

    @pytest.mark.asyncio
    async def test_stream_event_thinking_delta(self):
        event = MagicMock()
        type(event).__name__ = "StreamEvent"
        event.event = {
            "type": "content_block_delta",
            "delta": {"type": "thinking_delta", "thinking": "hmm"},
        }

        async def source():
            yield event

        adapter = ClaudeIteratorAdapter(source())
        chunks = []
        async for c in adapter:
            chunks.append(c)
        assert chunks[0].kind == ChunkKind.THINKING_DELTA

    @pytest.mark.asyncio
    async def test_stream_event_input_json_delta(self):
        event = MagicMock()
        type(event).__name__ = "StreamEvent"
        event.event = {
            "type": "content_block_delta",
            "delta": {"type": "input_json_delta", "partial_json": '{"key":'},
        }

        async def source():
            yield event

        adapter = ClaudeIteratorAdapter(source())
        chunks = []
        async for c in adapter:
            chunks.append(c)
        assert chunks[0].kind == ChunkKind.TOOL_USE_DELTA

    @pytest.mark.asyncio
    async def test_stream_event_content_block_start_tool_use(self):
        event = MagicMock()
        type(event).__name__ = "StreamEvent"
        event.event = {
            "type": "content_block_start",
            "content_block": {"type": "tool_use", "name": "bash"},
        }

        async def source():
            yield event

        adapter = ClaudeIteratorAdapter(source())
        chunks = []
        async for c in adapter:
            chunks.append(c)
        assert chunks[0].kind == ChunkKind.TOOL_USE_START
        assert chunks[0].tool_name == "bash"

    @pytest.mark.asyncio
    async def test_stream_event_no_event_attr(self):
        event = MagicMock(spec=[])
        type(event).__name__ = "StreamEvent"

        result_msg = MagicMock()
        type(result_msg).__name__ = "ResultMessage"

        async def source():
            yield event
            yield result_msg

        adapter = ClaudeIteratorAdapter(source())
        chunks = []
        async for c in adapter:
            chunks.append(c)
        assert len(chunks) == 1
        assert chunks[0].kind == ChunkKind.DONE

    @pytest.mark.asyncio
    async def test_multiple_content_blocks(self):
        text_block = MagicMock()
        type(text_block).__name__ = "TextBlock"
        text_block.text = "part1"

        tool_block = MagicMock()
        type(tool_block).__name__ = "ToolUseBlock"
        tool_block.name = "tool1"

        msg = MagicMock()
        type(msg).__name__ = "AssistantMessage"
        msg.content = [text_block, tool_block]

        async def source():
            yield msg

        adapter = ClaudeIteratorAdapter(source())
        chunks = []
        async for c in adapter:
            chunks.append(c)
        assert len(chunks) == 2
        assert chunks[0].kind == ChunkKind.TEXT_DELTA
        assert chunks[1].kind == ChunkKind.TOOL_USE_START

    @pytest.mark.asyncio
    async def test_assistant_message_no_content(self):
        msg = MagicMock(spec=[])  # no .content
        type(msg).__name__ = "AssistantMessage"

        result_msg = MagicMock()
        type(result_msg).__name__ = "ResultMessage"

        async def source():
            yield msg
            yield result_msg

        adapter = ClaudeIteratorAdapter(source())
        chunks = []
        async for c in adapter:
            chunks.append(c)
        assert len(chunks) == 1
        assert chunks[0].kind == ChunkKind.DONE
