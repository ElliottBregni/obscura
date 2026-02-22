"""
obscura.internal.stream — Streaming adapters for normalizing backend output.

Copilot is event/push-based (register callbacks, events fire).
Claude is pull-based (async iterator of Messages/StreamEvents).

Both are normalized to ``AsyncIterator[StreamChunk]``.
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator

from obscura.core.types import ChunkKind, StreamChunk, StreamMetadata


# ---------------------------------------------------------------------------
# Copilot: Event → AsyncIterator bridge
# ---------------------------------------------------------------------------


class EventToIteratorBridge:
    """Adapts Copilot's push-based events into an async iterator of StreamChunks.

    Usage in CopilotBackend::

        bridge = EventToIteratorBridge()
        session.on("assistant.message_delta", bridge.on_text_delta)
        session.on("session.idle", bridge.finish)
        await session.send(prompt)
        async for chunk in bridge:
            yield chunk
    """

    def __init__(self) -> None:
        self._queue: asyncio.Queue[StreamChunk | None] = asyncio.Queue()

    # -- Push methods (called from Copilot event handlers) ------------------

    def push(self, chunk: StreamChunk) -> None:
        """Push a normalized chunk into the queue."""
        self._queue.put_nowait(chunk)

    def on_text_delta(self, event: Any) -> None:
        """Map Copilot ``assistant.message_delta`` event."""
        delta = ""
        if (
            hasattr(event, "data")
            and hasattr(event.data, "delta_content")
            and event.data.delta_content
        ):
            delta = event.data.delta_content
        elif (
            hasattr(event, "data")
            and hasattr(event.data, "content")
            and event.data.content
        ):
            delta = event.data.content
        elif (
            hasattr(event, "data") and hasattr(event.data, "delta") and event.data.delta
        ):
            delta = event.data.delta
        elif isinstance(event, str):
            delta = event
        if delta:
            self.push(
                StreamChunk(
                    kind=ChunkKind.TEXT_DELTA,
                    text=delta,
                    raw=event,
                    native_event=event,
                )
            )

    def on_thinking_delta(self, event: Any) -> None:
        """Map Copilot ``assistant.reasoning_delta`` event."""
        delta = ""
        if (
            hasattr(event, "data")
            and hasattr(event.data, "delta_content")
            and event.data.delta_content
        ):
            delta = event.data.delta_content
        elif (
            hasattr(event, "data")
            and hasattr(event.data, "reasoning_text")
            and event.data.reasoning_text
        ):
            delta = event.data.reasoning_text
        elif (
            hasattr(event, "data") and hasattr(event.data, "delta") and event.data.delta
        ):
            delta = event.data.delta
        elif isinstance(event, str):
            delta = event
        self.push(
            StreamChunk(
                kind=ChunkKind.THINKING_DELTA,
                text=delta,
                raw=event,
                native_event=event,
            )
        )

    def on_tool_start(self, event: Any) -> None:
        """Map tool execution start."""
        name = ""
        if (
            hasattr(event, "data")
            and hasattr(event.data, "tool_name")
            and event.data.tool_name
        ):
            name = event.data.tool_name
        elif hasattr(event, "data") and hasattr(event.data, "name") and event.data.name:
            name = event.data.name
        self.push(
            StreamChunk(
                kind=ChunkKind.TOOL_USE_START,
                tool_name=name,
                raw=event,
                native_event=event,
            )
        )

    def on_tool_end(self, event: Any) -> None:
        """Map tool execution end."""
        name = ""
        if (
            hasattr(event, "data")
            and hasattr(event.data, "tool_name")
            and event.data.tool_name
        ):
            name = event.data.tool_name
        elif hasattr(event, "data") and hasattr(event.data, "name") and event.data.name:
            name = event.data.name
        self.push(
            StreamChunk(
                kind=ChunkKind.TOOL_USE_END,
                tool_name=name,
                raw=event,
                native_event=event,
            )
        )

    def finish(
        self, event: Any = None, *, metadata: StreamMetadata | None = None
    ) -> None:
        """Signal end of stream."""
        self.push(
            StreamChunk(
                kind=ChunkKind.DONE,
                raw=event,
                metadata=metadata,
                native_event=event,
            )
        )
        self._queue.put_nowait(None)

    def error(self, err: Exception | Any) -> None:
        """Signal error and end stream."""
        self.push(
            StreamChunk(
                kind=ChunkKind.ERROR,
                text=str(err),
                raw=err,
                native_event=err,
            )
        )
        self._queue.put_nowait(None)

    # -- AsyncIterator interface --------------------------------------------

    def __aiter__(self) -> AsyncIterator[StreamChunk]:
        return self

    async def __anext__(self) -> StreamChunk:
        item = await self._queue.get()
        if item is None:
            raise StopAsyncIteration
        return item


# ---------------------------------------------------------------------------
# Claude: Message iterator → StreamChunk iterator
# ---------------------------------------------------------------------------


class ClaudeIteratorAdapter:
    """Wraps Claude Agent SDK's ``AsyncIterator[Message]`` into
    ``AsyncIterator[StreamChunk]``.

    Claude yields different message types:
    - ``AssistantMessage`` with content blocks (TextBlock, ThinkingBlock, etc.)
    - ``StreamEvent`` with partial deltas (when include_partial_messages=True)
    - ``ResultMessage`` signalling completion

    This adapter normalizes all of them to StreamChunk.
    """

    def __init__(self, source: AsyncIterator[Any]) -> None:
        self._source = source
        self._buffer: list[StreamChunk] = []

    def __aiter__(self) -> AsyncIterator[StreamChunk]:
        return self

    async def __anext__(self) -> StreamChunk:
        # Drain buffer first
        if self._buffer:
            return self._buffer.pop(0)

        # Pull next message from Claude
        try:
            item = await self._source.__anext__()
        except StopAsyncIteration:
            raise

        chunks = self._adapt(item)
        if not chunks:
            # Skip empty messages, try next
            return await self.__anext__()

        # Return first chunk, buffer the rest
        self._buffer.extend(chunks[1:])
        return chunks[0]

    def _adapt(self, item: Any) -> list[StreamChunk]:
        """Convert a Claude message/event to one or more StreamChunks."""
        type_name = type(item).__name__

        # ResultMessage → done with metadata
        if type_name == "ResultMessage":
            meta = self._extract_result_metadata(item)
            return [
                StreamChunk(
                    kind=ChunkKind.DONE,
                    raw=item,
                    metadata=meta,
                    native_event=item,
                )
            ]

        # StreamEvent → partial deltas
        if type_name == "StreamEvent":
            return self._adapt_stream_event(item)

        # AssistantMessage → extract content blocks
        if type_name == "AssistantMessage":
            return self._adapt_content_blocks(item)

        # SystemMessage → skip (internal)
        if type_name == "SystemMessage":
            return []

        # UserMessage → skip
        if type_name == "UserMessage":
            return []

        # Unknown → text fallback
        return [
            StreamChunk(
                kind=ChunkKind.TEXT_DELTA,
                text=str(item),
                raw=item,
                native_event=item,
            )
        ]

    def _adapt_stream_event(self, event: Any) -> list[StreamChunk]:
        """Adapt a Claude StreamEvent to StreamChunks."""
        # StreamEvent has .event dict with raw API data
        if not hasattr(event, "event"):
            return []

        ev = event.event
        ev_type = ev.get("type", "")

        if ev_type == "content_block_delta":
            delta = ev.get("delta", {})
            delta_type = delta.get("type", "")
            if delta_type == "text_delta":
                return [
                    StreamChunk(
                        kind=ChunkKind.TEXT_DELTA,
                        text=delta.get("text", ""),
                        raw=event,
                        native_event=event,
                    )
                ]
            if delta_type == "thinking_delta":
                return [
                    StreamChunk(
                        kind=ChunkKind.THINKING_DELTA,
                        text=delta.get("thinking", ""),
                        raw=event,
                        native_event=event,
                    )
                ]
            if delta_type == "input_json_delta":
                return [
                    StreamChunk(
                        kind=ChunkKind.TOOL_USE_DELTA,
                        tool_input_delta=delta.get("partial_json", ""),
                        raw=event,
                        native_event=event,
                    )
                ]

        if ev_type == "content_block_start":
            block = ev.get("content_block", {})
            if block.get("type") == "tool_use":
                return [
                    StreamChunk(
                        kind=ChunkKind.TOOL_USE_START,
                        tool_name=block.get("name", ""),
                        tool_use_id=block.get("id", ""),
                        raw=event,
                        native_event=event,
                    )
                ]

        if ev_type == "content_block_stop":
            return [
                StreamChunk(
                    kind=ChunkKind.TOOL_USE_END,
                    raw=event,
                    native_event=event,
                )
            ]

        if ev_type == "message_start":
            return [
                StreamChunk(
                    kind=ChunkKind.MESSAGE_START,
                    raw=event,
                    native_event=event,
                )
            ]

        return []

    def _adapt_content_blocks(self, msg: Any) -> list[StreamChunk]:
        """Adapt an AssistantMessage's content blocks to StreamChunks."""
        chunks: list[StreamChunk] = []
        if not hasattr(msg, "content"):
            return chunks

        for block in msg.content:
            block_type = type(block).__name__

            if block_type == "TextBlock" and hasattr(block, "text"):
                chunks.append(
                    StreamChunk(
                        kind=ChunkKind.TEXT_DELTA,
                        text=block.text,
                        raw=block,
                        native_event=block,
                    )
                )
            elif block_type == "ThinkingBlock" and hasattr(block, "thinking"):
                chunks.append(
                    StreamChunk(
                        kind=ChunkKind.THINKING_DELTA,
                        text=block.thinking,
                        raw=block,
                        native_event=block,
                    )
                )
            elif block_type == "ToolUseBlock":
                chunks.append(
                    StreamChunk(
                        kind=ChunkKind.TOOL_USE_START,
                        tool_name=getattr(block, "name", ""),
                        raw=block,
                        native_event=block,
                    )
                )
            elif block_type == "ToolResultBlock":
                text = ""
                if hasattr(block, "content") and isinstance(block.content, str):
                    text = block.content
                chunks.append(
                    StreamChunk(
                        kind=ChunkKind.TOOL_RESULT,
                        text=text,
                        raw=block,
                        native_event=block,
                    )
                )

        return chunks

    @staticmethod
    def _extract_result_metadata(item: Any) -> StreamMetadata:
        """Extract StreamMetadata from a Claude ResultMessage."""
        usage: dict[str, int] | None = None
        if hasattr(item, "usage") and item.usage is not None:
            u = item.usage
            usage = {
                "input_tokens": getattr(u, "input_tokens", 0),
                "output_tokens": getattr(u, "output_tokens", 0),
            }
        return StreamMetadata(
            finish_reason=getattr(item, "stop_reason", "") or "",
            usage=usage,
            model_id=getattr(item, "model", "") or "",
            session_id=getattr(item, "session_id", "") or "",
        )
