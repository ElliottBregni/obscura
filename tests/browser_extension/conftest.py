"""Fixtures for browser extension host tests.

These tests exercise the native-messaging host by simulating Chrome's
stdin/stdout framing protocol. No actual Chrome instance is needed.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import struct
import sys
from pathlib import Path
from typing import Any, AsyncGenerator

import pytest
import pytest_asyncio

HOST_SCRIPT = (
    Path(__file__).parent.parent.parent
    / "packages"
    / "browser-extension"
    / "native-host"
    / "obscura_native_host.py"
)

BROWSER_TOOLS_DIR = HOST_SCRIPT.parent


def encode_frame(obj: dict[str, Any]) -> bytes:
    """Encode a dict as a native-messaging frame (4-byte LE length + JSON)."""
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    return struct.pack("<I", len(data)) + data


def decode_frame(raw: bytes) -> tuple[dict[str, Any] | None, bytes]:
    """Decode one frame from a byte buffer. Returns (message, remaining)."""
    if len(raw) < 4:
        return None, raw
    (length,) = struct.unpack("<I", raw[:4])
    if len(raw) < 4 + length:
        return None, raw
    payload = raw[4 : 4 + length]
    msg = json.loads(payload.decode("utf-8"))
    return msg, raw[4 + length :]


class HostProcess:
    """Wrapper around a native host subprocess for testing."""

    def __init__(self, proc: asyncio.subprocess.Process):
        self.proc = proc
        self._buf = b""
        self._messages: list[dict[str, Any]] = []

    async def send(self, msg: dict[str, Any]) -> None:
        """Send a framed message to the host."""
        assert self.proc.stdin is not None
        self.proc.stdin.write(encode_frame(msg))
        await self.proc.stdin.drain()

    async def recv(self, timeout: float = 10.0) -> dict[str, Any]:
        """Read one framed message from the host."""
        assert self.proc.stdout is not None
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            # Try to decode from buffer first
            msg, self._buf = decode_frame(self._buf)
            if msg is not None:
                self._messages.append(msg)
                return msg
            # Read more data
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise TimeoutError(
                    f"No message received within {timeout}s. "
                    f"Buffer: {self._buf[:200]!r}"
                )
            try:
                chunk = await asyncio.wait_for(
                    self.proc.stdout.read(4096),
                    timeout=min(remaining, 2.0),
                )
                if not chunk:
                    raise EOFError("Host process closed stdout")
                self._buf += chunk
            except asyncio.TimeoutError:
                continue

    async def recv_until(
        self, type_: str, timeout: float = 15.0
    ) -> dict[str, Any]:
        """Read messages until one with the given type arrives."""
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                types_seen = [m.get("type") for m in self._messages[-10:]]
                raise TimeoutError(
                    f"Never got type={type_!r}. Last types: {types_seen}"
                )
            msg = await self.recv(timeout=remaining)
            if msg.get("type") == type_:
                return msg

    async def close(self) -> None:
        """Shut down the host cleanly."""
        try:
            await self.send({"type": "shutdown"})
        except Exception:
            pass
        # Give the process a moment to exit on its own after shutdown.
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self.proc.wait(), timeout=2.0)
        if self.proc.returncode is None:
            try:
                self.proc.terminate()
                await asyncio.wait_for(self.proc.wait(), timeout=5.0)
            except Exception:
                with contextlib.suppress(Exception):
                    self.proc.kill()

    @property
    def all_messages(self) -> list[dict[str, Any]]:
        return list(self._messages)


@pytest_asyncio.fixture
async def host() -> AsyncGenerator[HostProcess, None]:
    """Launch the native host as a subprocess and yield a HostProcess wrapper."""
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        str(HOST_SCRIPT),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    hp = HostProcess(proc)
    try:
        yield hp
    finally:
        await hp.close()
