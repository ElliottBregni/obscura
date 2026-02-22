"""Pre-built StreamChunk factories for tests.

Replaces scattered ``_make_text_chunks`` / ``_make_tool_call_chunks``
helpers with a consistent, importable API.

Usage::

    from obscura.testing.chunks import text_chunks, tool_call_chunks

    turn1 = text_chunks("Hello world")
    turn2 = tool_call_chunks("search", {"q": "test"})
"""

from __future__ import annotations

import json
from typing import Any

from obscura.core.types import ChunkKind, StreamChunk

__all__ = [
    "done_chunk",
    "error_chunk",
    "text_chunk",
    "text_chunks",
    "thinking_chunk",
    "thinking_chunks",
    "tool_call_chunks",
    "tool_end_chunk",
    "tool_start_chunk",
    "tool_delta_chunk",
]


# ---------------------------------------------------------------------------
# Atomic chunk constructors
# ---------------------------------------------------------------------------


def text_chunk(text: str) -> StreamChunk:
    """Single TEXT_DELTA chunk."""
    return StreamChunk(kind=ChunkKind.TEXT_DELTA, text=text)


def thinking_chunk(text: str) -> StreamChunk:
    """Single THINKING_DELTA chunk."""
    return StreamChunk(kind=ChunkKind.THINKING_DELTA, text=text)


def done_chunk() -> StreamChunk:
    """Terminal DONE chunk."""
    return StreamChunk(kind=ChunkKind.DONE)


def error_chunk(message: str) -> StreamChunk:
    """ERROR chunk."""
    return StreamChunk(kind=ChunkKind.ERROR, text=message)


def tool_start_chunk(
    tool_name: str,
    *,
    tool_use_id: str = "",
    raw: dict[str, Any] | None = None,
) -> StreamChunk:
    """TOOL_USE_START chunk."""
    return StreamChunk(
        kind=ChunkKind.TOOL_USE_START,
        tool_name=tool_name,
        tool_use_id=tool_use_id,
        raw=raw,
    )


def tool_delta_chunk(input_json: str) -> StreamChunk:
    """TOOL_USE_DELTA chunk with serialized JSON input."""
    return StreamChunk(kind=ChunkKind.TOOL_USE_DELTA, tool_input_delta=input_json)


def tool_end_chunk() -> StreamChunk:
    """TOOL_USE_END chunk."""
    return StreamChunk(kind=ChunkKind.TOOL_USE_END)


# ---------------------------------------------------------------------------
# Composite chunk sequences (match the old _make_* helpers)
# ---------------------------------------------------------------------------


def text_chunks(text: str) -> list[StreamChunk]:
    """Split *text* into word-level TEXT_DELTA chunks + DONE.

    Drop-in replacement for the old ``_make_text_chunks()``.

    >>> text_chunks("Hello world")
    [StreamChunk(TEXT_DELTA, "Hello "), StreamChunk(TEXT_DELTA, "world "), StreamChunk(DONE)]
    """
    chunks: list[StreamChunk] = []
    for word in text.split(" "):
        chunks.append(StreamChunk(kind=ChunkKind.TEXT_DELTA, text=word + " "))
    chunks.append(StreamChunk(kind=ChunkKind.DONE))
    return chunks


def thinking_chunks(text: str) -> list[StreamChunk]:
    """THINKING_DELTA chunk + DONE."""
    return [
        StreamChunk(kind=ChunkKind.THINKING_DELTA, text=text),
        StreamChunk(kind=ChunkKind.DONE),
    ]


def tool_call_chunks(
    tool_name: str,
    tool_input: dict[str, Any] | None = None,
    *,
    preceding_text: str = "",
) -> list[StreamChunk]:
    """Simulate the model calling a tool: optional text + START + DELTA + DONE.

    Drop-in replacement for the old ``_make_tool_call_chunks()``.
    """
    chunks: list[StreamChunk] = []
    if preceding_text:
        chunks.append(StreamChunk(kind=ChunkKind.TEXT_DELTA, text=preceding_text))
    chunks.append(StreamChunk(kind=ChunkKind.TOOL_USE_START, tool_name=tool_name))
    input_json = json.dumps(tool_input or {})
    chunks.append(StreamChunk(kind=ChunkKind.TOOL_USE_DELTA, tool_input_delta=input_json))
    chunks.append(StreamChunk(kind=ChunkKind.DONE))
    return chunks
