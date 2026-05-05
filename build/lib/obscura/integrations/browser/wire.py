"""wire — shared framing and frame schemas for the browser bridge socket.

The native host and any Python client speak length-prefixed JSON over a
Unix socket. Keeping the framing here means a single source of truth: if
the wire format ever changes (e.g. switch to MessagePack), both sides
move together.
"""

from __future__ import annotations

import asyncio
import json
import logging
import struct
from typing import Any, Final, cast

log = logging.getLogger("obscura.browser.wire")

# 16 MiB — far above any realistic browser-tool payload, far below RAM
# pressure. Defends against malformed length headers from a misbehaving
# peer.
MAX_FRAME: Final[int] = 16 * 1024 * 1024


def encode_frame(obj: dict[str, Any]) -> bytes:
    """Serialise a frame to bytes with a 4-byte LE length prefix."""
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    return struct.pack("<I", len(body)) + body


async def read_frame(reader: asyncio.StreamReader) -> dict[str, Any] | None:
    """Read one length-prefixed JSON frame. Returns None on clean EOF."""
    try:
        header = await reader.readexactly(4)
    except asyncio.IncompleteReadError:
        log.debug("suppressed exception in read_frame", exc_info=True)
        return None
    (length,) = struct.unpack("<I", header)
    if length == 0:
        return {}
    if length > MAX_FRAME:
        msg = f"frame too large: {length} bytes"
        raise ValueError(msg)
    payload = await reader.readexactly(length)
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except json.JSONDecodeError:
        log.exception("invalid JSON frame")
        return {}
    if not isinstance(decoded, dict):
        return {}
    return cast("dict[str, Any]", decoded)


__all__ = ["MAX_FRAME", "encode_frame", "read_frame"]
