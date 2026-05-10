"""Convert Codex SDK thread items and notifications into Obscura types."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from obscura.core.enums.agent import ChunkKind
from obscura.core.models.content import TextBlock, ThinkingBlock, ToolUseBlock
from obscura.core.types import StreamChunk

logger = logging.getLogger(__name__)


def sanitize_tool_name(name: str) -> str:
    """Sanitize tool name to match API pattern ^[a-zA-Z0-9_-]{1,128}$."""
    return re.sub(r"[^a-zA-Z0-9_-]", "_", name)[:128]


def unwrap_item(item: Any) -> Any:
    """Unwrap a pydantic RootModel wrapper (``ThreadItem.root``) if present."""
    if item is None:
        return None
    root = getattr(item, "root", None)
    return root if root is not None else item


def summarize_file_changes(item: Any) -> str:
    """Render a ``fileChange`` item's changes as a short comma-separated string."""
    raw: Any = getattr(item, "changes", None) or []
    parts: list[str] = []
    for change in raw:
        c: Any = change
        kind = getattr(c, "kind", None) or getattr(c, "type", "?")
        path = getattr(c, "path", "?")
        parts.append(f"{kind}:{path}")
    return ", ".join(parts)


def map_notification_to_chunks(method: str, payload: Any) -> list[StreamChunk]:
    """Map a Codex app-server notification to zero or more StreamChunks."""
    if payload is None:
        return []

    if method == "item/agentMessage/delta":
        delta = getattr(payload, "delta", "")
        if not delta:
            return []
        return [StreamChunk(kind=ChunkKind.TEXT_DELTA, text=delta, raw=payload)]

    if method in (
        "item/reasoning/textDelta",
        "item/reasoning/summaryTextDelta",
    ):
        delta = getattr(payload, "delta", "")
        if not delta:
            return []
        return [StreamChunk(kind=ChunkKind.THINKING_DELTA, text=delta, raw=payload)]

    if method in ("item/started", "item/completed"):
        item = unwrap_item(getattr(payload, "item", None))
        if item is None:
            return []
        started = method == "item/started"
        item_type = getattr(item, "type", "")
        if item_type == "commandExecution":
            return command_execution_chunks(item, started=started)
        if item_type == "mcpToolCall":
            return mcp_tool_call_chunks(item, started=started)
        if item_type == "fileChange" and not started:
            return file_change_chunks(item)
        if item_type == "webSearch":
            return web_search_chunks(item, started=started)
        return []

    if method == "error":
        err = getattr(payload, "error", None)
        msg = getattr(err, "message", None) or "Unknown error"
        return [StreamChunk(kind=ChunkKind.ERROR, text=msg, raw=payload)]

    return []


def command_execution_chunks(item: Any, *, started: bool) -> list[StreamChunk]:
    item_id = getattr(item, "id", "") or ""
    if started:
        chunks = [
            StreamChunk(
                kind=ChunkKind.TOOL_USE_START,
                tool_name="shell_command",
                tool_use_id=item_id,
                raw=item,
            ),
        ]
        cmd = getattr(item, "command", "")
        if cmd:
            chunks.append(
                StreamChunk(
                    kind=ChunkKind.TOOL_USE_DELTA,
                    tool_input_delta=json.dumps({"command": cmd}),
                    raw=item,
                ),
            )
        return chunks

    output = getattr(item, "aggregated_output", "") or ""
    exit_code = getattr(item, "exit_code", None)
    text = output[:4096]
    if exit_code is not None:
        text = f"{text}\n[exit_code: {exit_code}]"
    return [
        StreamChunk(
            kind=ChunkKind.TOOL_RESULT,
            text=text,
            tool_use_id=item_id,
            raw=item,
        ),
        StreamChunk(
            kind=ChunkKind.TOOL_USE_END,
            tool_name="shell_command",
            tool_use_id=item_id,
            raw=item,
        ),
    ]


def mcp_tool_call_chunks(item: Any, *, started: bool) -> list[StreamChunk]:
    item_id = getattr(item, "id", "") or ""
    server = getattr(item, "server", "") or ""
    tool = getattr(item, "tool", "") or ""
    name = sanitize_tool_name(f"{server}_{tool}")
    if started:
        chunks: list[StreamChunk] = [
            StreamChunk(
                kind=ChunkKind.TOOL_USE_START,
                tool_name=name,
                tool_use_id=item_id,
                raw=item,
            ),
        ]
        args = getattr(item, "arguments", None)
        if args is not None:
            try:
                args_str = args if isinstance(args, str) else json.dumps(args)
            except (TypeError, ValueError):
                logger.debug("suppressed exception in mcp_tool_call_chunks", exc_info=True)
                args_str = str(args)
            chunks.append(
                StreamChunk(
                    kind=ChunkKind.TOOL_USE_DELTA,
                    tool_input_delta=args_str,
                    raw=item,
                ),
            )
        return chunks

    error = getattr(item, "error", None)
    result_obj = getattr(item, "result", None)
    if error is not None:
        text = f"Error: {getattr(error, 'message', None) or error}"
    elif result_obj is not None:
        content = getattr(result_obj, "content", None)
        try:
            text = json.dumps(content) if content is not None else ""
        except (TypeError, ValueError):
            logger.debug("suppressed exception in mcp_tool_call_chunks", exc_info=True)
            text = str(content)
    else:
        text = ""
    return [
        StreamChunk(
            kind=ChunkKind.TOOL_RESULT,
            text=text[:4096],
            tool_use_id=item_id,
            raw=item,
        ),
        StreamChunk(
            kind=ChunkKind.TOOL_USE_END,
            tool_name=name,
            tool_use_id=item_id,
            raw=item,
        ),
    ]


def file_change_chunks(item: Any) -> list[StreamChunk]:
    item_id = getattr(item, "id", "") or ""
    summary = summarize_file_changes(item)
    return [
        StreamChunk(
            kind=ChunkKind.TOOL_USE_START,
            tool_name="file_change",
            tool_use_id=item_id,
            raw=item,
        ),
        StreamChunk(
            kind=ChunkKind.TOOL_RESULT,
            text=summary,
            tool_use_id=item_id,
            raw=item,
        ),
        StreamChunk(
            kind=ChunkKind.TOOL_USE_END,
            tool_name="file_change",
            tool_use_id=item_id,
            raw=item,
        ),
    ]


def web_search_chunks(item: Any, *, started: bool) -> list[StreamChunk]:
    item_id = getattr(item, "id", "") or ""
    query = getattr(item, "query", "")
    if started:
        chunks = [
            StreamChunk(
                kind=ChunkKind.TOOL_USE_START,
                tool_name="web_search",
                tool_use_id=item_id,
                raw=item,
            ),
        ]
        if query:
            chunks.append(
                StreamChunk(
                    kind=ChunkKind.TOOL_USE_DELTA,
                    tool_input_delta=json.dumps({"query": query}),
                    raw=item,
                ),
            )
        return chunks
    return [
        StreamChunk(
            kind=ChunkKind.TOOL_USE_END,
            tool_name="web_search",
            tool_use_id=item_id,
            raw=item,
        ),
    ]


def items_to_content_blocks(items: list[Any], final_text: str) -> list[Any]:
    """Convert Codex thread items into Obscura content blocks."""
    blocks: list[Any] = []
    has_text = False

    for raw in items:
        item = unwrap_item(raw)
        if item is None:
            continue
        item_type = getattr(item, "type", "")

        if item_type == "agentMessage":
            blocks.append(TextBlock(text=getattr(item, "text", "") or ""))
            has_text = True

        elif item_type == "reasoning":
            content = list(getattr(item, "content", None) or [])
            summary = list(getattr(item, "summary", None) or [])
            text = "\n".join(str(s) for s in content + summary)
            if text:
                blocks.append(ThinkingBlock(text=text))

        elif item_type == "commandExecution":
            blocks.append(
                ToolUseBlock(
                    tool_name="shell_command",
                    args={"command": getattr(item, "command", "") or ""},
                    tool_use_id=getattr(item, "id", "") or "",
                ),
            )

        elif item_type == "mcpToolCall":
            server = getattr(item, "server", "") or ""
            tool = getattr(item, "tool", "") or ""
            blocks.append(
                ToolUseBlock(
                    tool_name=sanitize_tool_name(f"{server}_{tool}"),
                    args=getattr(item, "arguments", None) or {},
                    tool_use_id=getattr(item, "id", "") or "",
                ),
            )

        elif item_type == "fileChange":
            blocks.append(
                ToolUseBlock(
                    tool_name="file_change",
                    args={"changes": summarize_file_changes(item)},
                    tool_use_id=getattr(item, "id", "") or "",
                ),
            )

    if not has_text:
        blocks.insert(0, TextBlock(text=final_text))

    return blocks
