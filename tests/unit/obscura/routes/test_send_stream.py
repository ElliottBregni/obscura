"""Tests for the REST stream payload serializer."""

from __future__ import annotations

import json

from obscura.core.enums.agent import ChunkKind
from obscura.core.types import StreamChunk, StreamMetadata
from obscura.routes.send import _stream_chunk_event


def test_stream_chunk_event_includes_kind_and_metadata_object() -> None:
    chunk = StreamChunk(
        kind=ChunkKind.TEXT_DELTA,
        text="hello",
        tool_name="",
        tool_input_delta="",
        tool_use_id="",
        metadata=StreamMetadata(
            finish_reason="stop",
            usage={"input_tokens": 3, "output_tokens": 2},
            model_id="model-1",
            session_id="session-1",
        ),
    )

    event = _stream_chunk_event(chunk)
    assert event["event"] == "text_delta"

    payload = json.loads(event["data"])
    assert payload["kind"] == "text_delta"
    assert payload["text"] == "hello"
    assert payload["metadata"] == {
        "finish_reason": "stop",
        "usage": {"input_tokens": 3, "output_tokens": 2},
        "model_id": "model-1",
        "session_id": "session-1",
    }
