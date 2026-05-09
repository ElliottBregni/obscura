"""obscura.internal.stream — Streaming adapters for normalizing backend output.

Copilot is event/push-based (register callbacks, events fire).
Claude is pull-based (async iterator of Messages/StreamEvents).

Both are normalized to ``AsyncIterator[StreamChunk]``.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any, cast

from obscura.core.enums.agent import ChunkKind
from obscura.core.types import StreamChunk, StreamMetadata
import logging

logger = logging.getLogger(__name__)


if TYPE_CHECKING:
    from collections.abc import AsyncIterator


def _extract_sdk_tool_result(data: Any) -> tuple[str, bool, bool]:
    """Pull a tool-execution result out of an SDK completion event.

    Returns ``(text, has_result, success)``.

    Copilot SDK 0.3 puts ``result`` (a ``{"content": str, ...}`` dict) and
    ``success: bool`` on ``data``. Older builds may use ``output`` or carry
    raw text. ``has_result=False`` means the caller should not synthesise a
    TOOL_RESULT chunk.
    """
    if data is None:
        return "", False, True

    def _get(name: str) -> Any:
        val = getattr(data, name, None)
        if val is None and isinstance(data, dict):
            val = cast(dict[str, Any], data).get(name)
        return val

    success = _get("success")
    if success is None:
        success = True
    raw_result = _get("result")
    if raw_result is None:
        raw_result = _get("output")
    if raw_result is None:
        return "", False, bool(success)

    text: str | None = None
    if isinstance(raw_result, str):
        text = raw_result
    elif isinstance(raw_result, dict):
        result_dict = cast(dict[str, Any], raw_result)
        candidate = result_dict.get("content")
        if not isinstance(candidate, str):
            candidate = result_dict.get("detailedContent")
        if isinstance(candidate, str):
            text = candidate
        else:
            try:
                text = json.dumps(result_dict)
            except (TypeError, ValueError):
                logger.debug(
                    "_extract_sdk_tool_result: json.dumps(result dict) failed",
                    exc_info=True,
                )
                text = str(result_dict)
    else:
        # Pydantic-style model (Copilot SDK ``ToolExecutionCompleteResult``):
        # check for ``content`` / ``detailedContent`` attributes before
        # falling back to a JSON dump (which would emit a useless ``repr``).
        candidate = getattr(raw_result, "content", None)
        if not isinstance(candidate, str):
            candidate = getattr(raw_result, "detailedContent", None)
        if isinstance(candidate, str):
            text = candidate
        else:
            try:
                text = json.dumps(raw_result, default=str)
            except (TypeError, ValueError):
                logger.debug(
                    "_extract_sdk_tool_result: json.dumps(model) failed",
                    exc_info=True,
                )
                text = str(cast(object, raw_result))

    return text or "", True, bool(success)


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
        # Track the most recent tool_use_id so DELTA/END chunks can carry
        # the same id the START emitted. Copilot's tool events may put the
        # id only on the start event; without this, agent_loop_v2 keys
        # partial_inputs on an empty string and drops the input. Also
        # used to backfill an end-event tool_name when the SDK omits it.
        self._active_tool_id: str = ""
        self._active_tool_name: str = ""
        # Cumulative text we've already emitted for this turn. Used to
        # detect providers that misuse delta events to send cumulative
        # content (Copilot's `assistant.message_delta` has been observed
        # to do this on certain message shapes). When the next "delta"
        # is a prefix-extension of what we've sent, we slice off the
        # duplicate and emit only the new tail.
        self._emitted_text: str = ""
        self._emitted_thinking: str = ""

    # -- Push methods (called from Copilot event handlers) ------------------

    def push(self, chunk: StreamChunk) -> None:
        """Push a normalized chunk into the queue."""
        self._queue.put_nowait(chunk)

    def on_text_delta(self, event: Any) -> None:
        """Map Copilot ``assistant.message_delta`` event.

        A delta event MUST carry incremental new chars, not cumulative
        content. We accept (in priority order) ``delta_content`` →
        ``delta`` → bare-string events. The legacy ``content`` field
        was a cumulative-snapshot fallback that double-printed assistant
        text whenever Copilot's SDK shipped a delta event without a
        proper ``delta_content`` field; we now log and drop those.

        As defense in depth, _normalize_delta detects when the
        incoming delta is a prefix-extension of what we've already
        emitted (i.e. a provider misusing delta semantics to send
        cumulative content) and trims it down to just the new tail.
        """
        raw_delta = ""
        if (
            hasattr(event, "data")
            and hasattr(event.data, "delta_content")
            and event.data.delta_content
        ):
            raw_delta = event.data.delta_content
        elif (
            hasattr(event, "data") and hasattr(event.data, "delta") and event.data.delta
        ):
            raw_delta = event.data.delta
        elif isinstance(event, str):
            raw_delta = event
        else:
            # Some Copilot SDK builds emit assistant.message_delta with
            # only `content` (cumulative) and no `delta_content`. Don't
            # silently treat that as an incremental delta — it's the bug
            # that caused the duplicate-render symptom. Log loudly so
            # we can spot any new provider regression.
            if (
                hasattr(event, "data")
                and hasattr(event.data, "content")
                and event.data.content
            ):
                logger.warning(
                    "assistant.message_delta missing delta_content; "
                    "ignoring cumulative `content` field to avoid "
                    "double-print (data=%r)",
                    type(event.data).__name__,
                )
            return

        delta = self._normalize_delta(raw_delta, kind="text")
        if delta:
            self._emitted_text += delta
            self.push(
                StreamChunk(
                    kind=ChunkKind.TEXT_DELTA,
                    text=delta,
                    raw=event,
                    native_event=event,
                ),
            )

    def _normalize_delta(self, raw_delta: str, *, kind: str) -> str:
        """Strip the prefix-overlap when a provider sends cumulative content.

        If ``raw_delta`` starts with everything we've already emitted in
        this turn, the provider sent the full message-so-far instead of
        just the new chars. Slice the overlap and return only the tail.
        Logs a warning the first time it triggers per turn so the
        regression is visible.
        """
        if not raw_delta:
            return ""
        emitted = self._emitted_text if kind == "text" else self._emitted_thinking
        if emitted and raw_delta.startswith(emitted):
            tail = raw_delta[len(emitted) :]
            logger.warning(
                "%s delta arrived as cumulative snapshot (len=%d, "
                "overlap=%d, tail=%d); emitting tail only",
                kind,
                len(raw_delta),
                len(emitted),
                len(tail),
            )
            return tail
        return raw_delta

    def on_thinking_delta(self, event: Any) -> None:
        """Map Copilot ``assistant.reasoning_delta`` event."""
        raw_delta = ""
        if (
            hasattr(event, "data")
            and hasattr(event.data, "delta_content")
            and event.data.delta_content
        ):
            raw_delta = event.data.delta_content
        elif (
            hasattr(event, "data")
            and hasattr(event.data, "reasoning_text")
            and event.data.reasoning_text
        ):
            # reasoning_text is the cumulative-snapshot variant; let
            # _normalize_delta strip the overlap when used as a fallback.
            raw_delta = event.data.reasoning_text
        elif (
            hasattr(event, "data") and hasattr(event.data, "delta") and event.data.delta
        ):
            raw_delta = event.data.delta
        elif isinstance(event, str):
            raw_delta = event

        delta = self._normalize_delta(raw_delta, kind="thinking")
        if delta:
            self._emitted_thinking += delta
            self.push(
                StreamChunk(
                    kind=ChunkKind.THINKING_DELTA,
                    text=delta,
                    raw=event,
                    native_event=event,
                ),
            )

    def on_tool_start(self, event: Any) -> None:
        """Map tool execution start."""
        name = ""
        tool_id = ""
        data: Any = getattr(event, "data", None)
        if data is not None:
            # Copilot SDK 0.3 events carry camelCase fields (toolName,
            # toolCallId); older builds and other SDKs use snake_case.
            name = (
                getattr(data, "tool_name", "")
                or getattr(data, "toolName", "")
                or getattr(data, "name", "")
                or ""
            )
            # Copilot may surface the call id under a few different names;
            # try each in turn. Falls back to empty (agent_loop_v2 will
            # treat the call as un-cached, which is correct for siblings
            # that have no SDK identity).
            tool_id = (
                getattr(data, "tool_call_id", "")
                or getattr(data, "toolCallId", "")
                or getattr(data, "tool_use_id", "")
                or getattr(data, "toolUseId", "")
                or getattr(data, "call_id", "")
                or getattr(data, "callId", "")
                or getattr(data, "id", "")
                or ""
            )
            if isinstance(data, dict):
                data_dict = cast(dict[str, Any], data)
                if not name:
                    for key in ("tool_name", "toolName", "name"):
                        val = data_dict.get(key)
                        if val:
                            name = str(val)
                            break
                if not tool_id:
                    for key in (
                        "tool_call_id",
                        "toolCallId",
                        "tool_use_id",
                        "toolUseId",
                        "call_id",
                        "callId",
                        "id",
                    ):
                        val = data_dict.get(key)
                        if val:
                            tool_id = str(val)
                            break
        # Cache the active id/name so DELTA chunks emitted below and any
        # following on_tool_end can attribute back to the same call even
        # if Copilot only emits the id on the start event.
        self._active_tool_id = tool_id
        self._active_tool_name = name
        self.push(
            StreamChunk(
                kind=ChunkKind.TOOL_USE_START,
                tool_name=name,
                tool_use_id=tool_id,
                raw=event,
                native_event=event,
            ),
        )
        # Extract tool input from event and emit as TOOL_USE_DELTA so the
        # agent loop can parse arguments.  Copilot events carry input in
        # various attributes; try common locations.
        if data is not None:
            tool_input: Any = None
            for attr in (
                "tool_input",
                "toolInput",
                "input",
                "arguments",
                "parameters",
            ):
                val = getattr(cast(Any, data), attr, None)
                if val is not None:
                    tool_input = val
                    break
            # Also check dict-style access
            if tool_input is None and isinstance(data, dict):
                data_dict = cast(dict[str, Any], data)
                for key in (
                    "tool_input",
                    "toolInput",
                    "input",
                    "arguments",
                    "parameters",
                ):
                    if key in data_dict:
                        tool_input = data_dict[key]
                        break
            if tool_input is not None:
                if isinstance(tool_input, str):
                    delta = tool_input
                elif isinstance(tool_input, dict):
                    delta = json.dumps(cast(dict[str, Any], tool_input))
                else:
                    try:
                        delta = json.dumps(tool_input)
                    except (TypeError, ValueError):
                        logger.debug(
                            "suppressed exception in on_tool_start", exc_info=True
                        )
                        delta = str(cast(object, tool_input))
                self.push(
                    StreamChunk(
                        kind=ChunkKind.TOOL_USE_DELTA,
                        tool_use_id=tool_id,
                        tool_name=name,
                        tool_input_delta=delta,
                        raw=event,
                        native_event=event,
                    ),
                )

    def on_tool_end(self, event: Any) -> None:
        """Map tool execution end."""
        name = ""
        tool_id = ""
        data = getattr(event, "data", None)
        if data is not None:
            # Copilot SDK 0.3 emits camelCase (toolName, toolCallId);
            # earlier builds and other SDKs use snake_case.
            name = (
                getattr(data, "tool_name", "")
                or getattr(data, "toolName", "")
                or getattr(data, "name", "")
                or ""
            )
            tool_id = (
                getattr(data, "tool_call_id", "")
                or getattr(data, "toolCallId", "")
                or getattr(data, "tool_use_id", "")
                or getattr(data, "toolUseId", "")
                or getattr(data, "call_id", "")
                or getattr(data, "callId", "")
                or getattr(data, "id", "")
                or ""
            )
            if isinstance(data, dict):
                data_dict = cast(dict[str, Any], data)
                if not name:
                    for key in ("tool_name", "toolName", "name"):
                        val = data_dict.get(key)
                        if val:
                            name = str(val)
                            break
                if not tool_id:
                    for key in (
                        "tool_call_id",
                        "toolCallId",
                        "tool_use_id",
                        "toolUseId",
                        "call_id",
                        "callId",
                        "id",
                    ):
                        val = data_dict.get(key)
                        if val:
                            tool_id = str(val)
                            break
        # Backfill from the active call when the END event omits id/name —
        # copilot tool events are emitted in start/end pairs, so the most
        # recent active call is the right attribution.
        if not tool_id:
            tool_id = self._active_tool_id
        if not name:
            name = self._active_tool_name
        # Reset active state — any future call gets its own id/name pair.
        self._active_tool_id = ""
        self._active_tool_name = ""
        self.push(
            StreamChunk(
                kind=ChunkKind.TOOL_USE_END,
                tool_name=name,
                tool_use_id=tool_id,
                raw=event,
                native_event=event,
            ),
        )
        # When the backend SDK runs the tool itself (Copilot, Claude),
        # the completion event carries the result. Emit a TOOL_RESULT
        # chunk so agent_loop_v2 can short-circuit its own dispatch
        # rather than re-running the tool against the local Python
        # handler (which has caused signature mismatches and double
        # execution). The chunk's raw event preserves ``success`` so
        # the loop can tag the result as error or ok.
        result_text, has_result, _success = _extract_sdk_tool_result(data)
        if has_result:
            # ``success`` is read off the raw event in agent_loop_v2's
            # TOOL_RESULT handler — that keeps the wire shape (camelCase
            # ``success``) as the only source of truth for is_error tagging.
            self.push(
                StreamChunk(
                    kind=ChunkKind.TOOL_RESULT,
                    tool_name=name,
                    tool_use_id=tool_id,
                    text=result_text,
                    raw=event,
                    native_event=event,
                ),
            )

    def finish(
        self,
        event: Any = None,
        *,
        metadata: StreamMetadata | None = None,
    ) -> None:
        """Signal end of stream."""
        self.push(
            StreamChunk(
                kind=ChunkKind.DONE,
                raw=event,
                metadata=metadata,
                native_event=event,
            ),
        )
        self._queue.put_nowait(None)
        # Reset cumulative-detection state for the next stream lifecycle —
        # bridges are sometimes reused across turns by the same backend.
        self._emitted_text = ""
        self._emitted_thinking = ""

    def error(
        self,
        err: Exception | Any,
        *,
        metadata: StreamMetadata | None = None,
    ) -> None:
        """Signal error and end stream."""
        self.push(
            StreamChunk(
                kind=ChunkKind.ERROR,
                text=str(err),
                raw=err,
                native_event=err,
                metadata=metadata,
            ),
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
        # Claude's streaming protocol uses content_block.index to correlate
        # start/delta/stop events for the same block, but the SDK puts the
        # actual tool_use id only on content_block_start. We track the
        # index -> (id, name) map here so DELTA and END chunks can carry
        # the same tool_use_id the START emitted — without that, the agent
        # loop's partial_names / partial_inputs dicts (keyed on tool_use_id)
        # silently drop the streamed input and the final ToolCallInfo gets
        # constructed with empty name + input.
        self._index_to_tool: dict[int, tuple[str, str]] = {}

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
                ),
            ]

        # StreamEvent → partial deltas
        if type_name == "StreamEvent":
            return self._adapt_stream_event(item)

        # AssistantMessage → extract content blocks
        if type_name == "AssistantMessage":
            return self._adapt_content_blocks(item)

        # SystemMessage subclasses (TaskStartedMessage / TaskProgressMessage /
        # TaskNotificationMessage / MirrorErrorMessage) — surface as
        # structured chunks so the renderer can render a consistent system
        # notice instead of dropping them. Match by class name to avoid a
        # hard import dep on the SDK class hierarchy (which evolves between
        # releases).
        if type_name == "TaskStartedMessage":
            return [
                StreamChunk(
                    kind=ChunkKind.TASK_STARTED,
                    text=getattr(item, "description", "") or "",
                    tool_use_id=getattr(item, "task_id", "") or "",
                    raw=item,
                    native_event=item,
                ),
            ]
        if type_name == "TaskProgressMessage":
            return [
                StreamChunk(
                    kind=ChunkKind.TASK_PROGRESS,
                    text=getattr(item, "description", "") or "",
                    tool_name=getattr(item, "last_tool_name", "") or "",
                    tool_use_id=getattr(item, "task_id", "") or "",
                    raw=item,
                    native_event=item,
                ),
            ]
        if type_name == "TaskNotificationMessage":
            return [
                StreamChunk(
                    kind=ChunkKind.TASK_NOTIFICATION,
                    text=getattr(item, "summary", "") or "",
                    tool_use_id=getattr(item, "task_id", "") or "",
                    raw=item,
                    native_event=item,
                ),
            ]
        if type_name == "MirrorErrorMessage":
            return [
                StreamChunk(
                    kind=ChunkKind.MIRROR_ERROR,
                    text=getattr(item, "error", "") or "",
                    raw=item,
                    native_event=item,
                ),
            ]

        # SystemMessage (base class) → skip (internal). Subclasses handled
        # above; this only catches plain SystemMessage instances.
        if type_name == "SystemMessage":
            return []

        # UserMessage → skip
        if type_name == "UserMessage":
            return []

        # RateLimitEvent → surface as a structured chunk so the renderer
        # can decide what to do (status line, log, suppress) instead of
        # dumping repr() into the chat.
        if type_name == "RateLimitEvent":
            info = getattr(item, "rate_limit_info", None)
            status = getattr(info, "status", None)
            logger.info(
                "claude rate-limit: status=%s type=%s utilization=%s resets_at=%s",
                status,
                getattr(info, "rate_limit_type", None),
                getattr(info, "utilization", None),
                getattr(info, "resets_at", None),
            )
            return [
                StreamChunk(
                    kind=ChunkKind.RATE_LIMIT,
                    text=str(status) if status else "",
                    raw=item,
                    native_event=item,
                ),
            ]

        logger.warning(
            "ClaudeIteratorAdapter: unrecognized SDK message type %r; dropping",
            type_name,
        )
        return []

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
            block_index = ev.get("index")
            if delta_type == "text_delta":
                return [
                    StreamChunk(
                        kind=ChunkKind.TEXT_DELTA,
                        text=delta.get("text", ""),
                        raw=event,
                        native_event=event,
                    ),
                ]
            if delta_type == "thinking_delta":
                return [
                    StreamChunk(
                        kind=ChunkKind.THINKING_DELTA,
                        text=delta.get("thinking", ""),
                        raw=event,
                        native_event=event,
                    ),
                ]
            if delta_type == "input_json_delta":
                tool_id, tool_name = self._index_to_tool.get(block_index, ("", ""))
                return [
                    StreamChunk(
                        kind=ChunkKind.TOOL_USE_DELTA,
                        tool_use_id=tool_id,
                        tool_name=tool_name,
                        tool_input_delta=delta.get("partial_json", ""),
                        raw=event,
                        native_event=event,
                    ),
                ]

        if ev_type == "content_block_start":
            block = ev.get("content_block", {})
            if block.get("type") == "tool_use":
                tool_id = block.get("id", "")
                tool_name = block.get("name", "")
                idx = ev.get("index")
                if idx is not None:
                    self._index_to_tool[idx] = (tool_id, tool_name)
                return [
                    StreamChunk(
                        kind=ChunkKind.TOOL_USE_START,
                        tool_name=tool_name,
                        tool_use_id=tool_id,
                        raw=event,
                        native_event=event,
                    ),
                ]

        if ev_type == "content_block_stop":
            block_index = ev.get("index")
            tool_id, tool_name = self._index_to_tool.pop(block_index, ("", ""))
            # Only emit TOOL_USE_END for blocks we tracked as tool_use —
            # text/thinking blocks also produce content_block_stop events
            # and the agent loop's TOOL_USE_END handler would attribute an
            # empty ToolCallInfo for them otherwise.
            if not tool_id and not tool_name:
                return []
            return [
                StreamChunk(
                    kind=ChunkKind.TOOL_USE_END,
                    tool_use_id=tool_id,
                    tool_name=tool_name,
                    raw=event,
                    native_event=event,
                ),
            ]

        if ev_type == "message_start":
            return [
                StreamChunk(
                    kind=ChunkKind.MESSAGE_START,
                    raw=event,
                    native_event=event,
                ),
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
                    ),
                )
            elif block_type == "ThinkingBlock" and hasattr(block, "thinking"):
                chunks.append(
                    StreamChunk(
                        kind=ChunkKind.THINKING_DELTA,
                        text=block.thinking,
                        raw=block,
                        native_event=block,
                    ),
                )
            elif block_type == "ToolUseBlock":
                tool_id = getattr(block, "id", "")
                tool_name = getattr(block, "name", "")
                chunks.append(
                    StreamChunk(
                        kind=ChunkKind.TOOL_USE_START,
                        tool_name=tool_name,
                        tool_use_id=tool_id,
                        raw=block,
                        native_event=block,
                    ),
                )
                # Emit tool input as a single delta (mirrors streaming path).
                # Carry tool_use_id + tool_name so the loop's partial_inputs
                # dict (keyed on tool_use_id) can attribute the delta back to
                # the right START.
                block_input = getattr(block, "input", None)
                if block_input is not None:
                    chunks.append(
                        StreamChunk(
                            kind=ChunkKind.TOOL_USE_DELTA,
                            tool_use_id=tool_id,
                            tool_name=tool_name,
                            tool_input_delta=json.dumps(block_input),
                            raw=block,
                            native_event=block,
                        ),
                    )
                chunks.append(
                    StreamChunk(
                        kind=ChunkKind.TOOL_USE_END,
                        tool_use_id=tool_id,
                        tool_name=tool_name,
                        raw=block,
                        native_event=block,
                    ),
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
                    ),
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
