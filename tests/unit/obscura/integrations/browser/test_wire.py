"""Unit tests for the browser bridge wire format."""

from __future__ import annotations

import asyncio
import struct

import pytest

from obscura.integrations.browser.wire import MAX_FRAME, encode_frame, read_frame


def test_encode_frame_round_trip() -> None:
    payload = {"type": "hello", "n": 42, "s": "héllo"}
    raw = encode_frame(payload)
    assert len(raw) >= 4
    (length,) = struct.unpack("<I", raw[:4])
    assert length == len(raw) - 4


@pytest.mark.asyncio
async def test_read_frame_round_trip() -> None:
    payload = {"type": "tools", "tools": [{"name": "browser_x"}]}
    raw = encode_frame(payload)
    reader = asyncio.StreamReader()
    reader.feed_data(raw)
    reader.feed_eof()
    out = await read_frame(reader)
    assert out == payload


@pytest.mark.asyncio
async def test_read_frame_clean_eof() -> None:
    reader = asyncio.StreamReader()
    reader.feed_eof()
    assert await read_frame(reader) is None


@pytest.mark.asyncio
async def test_read_frame_zero_length() -> None:
    reader = asyncio.StreamReader()
    reader.feed_data(struct.pack("<I", 0))
    reader.feed_eof()
    assert await read_frame(reader) == {}


@pytest.mark.asyncio
async def test_read_frame_too_large_rejected() -> None:
    reader = asyncio.StreamReader()
    reader.feed_data(struct.pack("<I", MAX_FRAME + 1))
    reader.feed_eof()
    with pytest.raises(ValueError, match="frame too large"):
        await read_frame(reader)


@pytest.mark.asyncio
async def test_read_frame_invalid_json_returns_empty_dict() -> None:
    body = b"not json"
    reader = asyncio.StreamReader()
    reader.feed_data(struct.pack("<I", len(body)) + body)
    reader.feed_eof()
    # Per the wire contract, malformed payloads degrade to {} (logged) rather
    # than crashing the read loop.
    assert await read_frame(reader) == {}


@pytest.mark.asyncio
async def test_read_frame_non_object_returns_empty_dict() -> None:
    body = b"[1,2,3]"
    reader = asyncio.StreamReader()
    reader.feed_data(struct.pack("<I", len(body)) + body)
    reader.feed_eof()
    assert await read_frame(reader) == {}
