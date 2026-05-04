"""Tests for RetryingBackend — the transient-error retry wrapper."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from obscura.core.backend_retry import RetryingBackend
from obscura.core.enums.agent import ChunkKind
from obscura.core.types import BackendCapabilities, Message, StreamChunk


class _ScriptedBackend:
    """Backend stub that follows a per-attempt script.

    Each attempt either yields chunks then completes, raises before any
    chunk yields, or raises mid-stream. Attempts beyond the script length
    raise ``StopIteration``.
    """

    name = "scripted"
    capabilities = BackendCapabilities(supports_streaming=True)

    def __init__(self, script: list[Any]) -> None:
        # Each entry: list[StreamChunk | Exception]
        self.script = script
        self.attempts = 0

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def stream(
        self, messages: list[Message] | None = None, **_kwargs: Any
    ) -> AsyncIterator[StreamChunk]:
        idx = self.attempts
        self.attempts += 1
        if idx >= len(self.script):
            raise AssertionError("script exhausted")
        for item in self.script[idx]:
            if isinstance(item, BaseException):
                raise item
            yield item


# ---------------------------------------------------------------------------


class TestRetryingBackend:
    @pytest.mark.asyncio
    async def test_succeeds_first_try_no_retry(self) -> None:
        chunk = StreamChunk(kind=ChunkKind.TEXT_DELTA, text="hi")
        backend = _ScriptedBackend([[chunk]])
        wrapped = RetryingBackend(backend, max_retries=3, base_delay_s=0.0)
        chunks = [c async for c in wrapped.stream()]
        assert backend.attempts == 1
        assert len(chunks) == 1

    @pytest.mark.asyncio
    async def test_retries_on_transient_error_before_first_chunk(self) -> None:
        chunk = StreamChunk(kind=ChunkKind.TEXT_DELTA, text="hi")
        backend = _ScriptedBackend(
            [
                [ConnectionError("transient")],
                [TimeoutError("transient")],
                [chunk],
            ]
        )
        wrapped = RetryingBackend(backend, max_retries=3, base_delay_s=0.0)
        chunks = [c async for c in wrapped.stream()]
        assert backend.attempts == 3
        assert len(chunks) == 1

    @pytest.mark.asyncio
    async def test_mid_stream_retry_when_allow_mid_stream(self) -> None:
        """allow_mid_stream=True retries even after chunks have been yielded.
        Caller is responsible for dedup (AgentLoopV2._seen_calls handles
        this in the real path)."""
        c1 = StreamChunk(kind=ChunkKind.TEXT_DELTA, text="partial")
        c2 = StreamChunk(kind=ChunkKind.TEXT_DELTA, text="retry-success")
        backend = _ScriptedBackend(
            [
                [c1, ConnectionError("transient mid-stream")],
                [c2],
            ]
        )
        wrapped = RetryingBackend(
            backend, max_retries=3, base_delay_s=0.0, allow_mid_stream=True
        )
        chunks: list[StreamChunk] = []
        async for c in wrapped.stream():
            chunks.append(c)
        # Both attempts' chunks land — caller must dedupe semantically.
        assert backend.attempts == 2
        assert [c.text for c in chunks] == ["partial", "retry-success"]

    @pytest.mark.asyncio
    async def test_does_not_retry_after_first_chunk(self) -> None:
        """If a chunk has been yielded, retry would duplicate — re-raise."""
        chunk = StreamChunk(kind=ChunkKind.TEXT_DELTA, text="partial")
        backend = _ScriptedBackend(
            [
                [chunk, ConnectionError("transient mid-stream")],
                # Second attempt would have a clean stream, but we mustn't get here.
                [StreamChunk(kind=ChunkKind.TEXT_DELTA, text="never")],
            ]
        )
        wrapped = RetryingBackend(backend, max_retries=3, base_delay_s=0.0)

        chunks: list[StreamChunk] = []
        with pytest.raises(ConnectionError):
            async for c in wrapped.stream():
                chunks.append(c)
        assert backend.attempts == 1
        assert len(chunks) == 1
        assert chunks[0].text == "partial"

    @pytest.mark.asyncio
    async def test_non_transient_error_does_not_retry(self) -> None:
        backend = _ScriptedBackend(
            [
                [ValueError("not transient")],
                [StreamChunk(kind=ChunkKind.TEXT_DELTA, text="never")],
            ]
        )
        wrapped = RetryingBackend(backend, max_retries=3, base_delay_s=0.0)
        with pytest.raises(ValueError):
            async for _ in wrapped.stream():
                pass
        assert backend.attempts == 1

    @pytest.mark.asyncio
    async def test_exhausted_retries_propagates_last_error(self) -> None:
        backend = _ScriptedBackend(
            [
                [ConnectionError("a")],
                [ConnectionError("b")],
                [ConnectionError("c")],
            ]
        )
        wrapped = RetryingBackend(backend, max_retries=3, base_delay_s=0.0)
        with pytest.raises(ConnectionError, match="c"):
            async for _ in wrapped.stream():
                pass
        assert backend.attempts == 3

    @pytest.mark.asyncio
    async def test_custom_transient_set(self) -> None:
        class _MyTransient(Exception):
            pass

        chunk = StreamChunk(kind=ChunkKind.TEXT_DELTA, text="ok")
        backend = _ScriptedBackend(
            [
                [_MyTransient("custom")],
                [chunk],
            ]
        )
        wrapped = RetryingBackend(
            backend,
            max_retries=3,
            base_delay_s=0.0,
            transient_exceptions=(_MyTransient,),
        )
        chunks = [c async for c in wrapped.stream()]
        assert backend.attempts == 2
        assert len(chunks) == 1

    def test_passthrough_properties(self) -> None:
        backend = _ScriptedBackend([])
        wrapped = RetryingBackend(backend)
        # Capabilities forwarded.
        assert wrapped.capabilities.supports_streaming is True
        # Name forwarded.
        assert wrapped.name == "scripted"

    @pytest.mark.asyncio
    async def test_start_close_forwarded(self) -> None:
        calls: list[str] = []

        class _Tracking:
            name = "track"
            capabilities = BackendCapabilities()

            async def start(self) -> None:
                calls.append("start")

            async def close(self) -> None:
                calls.append("close")

            async def stream(
                self, messages: list[Message] | None = None, **_kwargs: Any
            ) -> AsyncIterator[StreamChunk]:
                if False:
                    yield None  # pragma: no cover

        wrapped = RetryingBackend(_Tracking())  # type: ignore[arg-type]
        await wrapped.start()
        await wrapped.close()
        assert calls == ["start", "close"]

    @pytest.mark.asyncio
    async def test_backoff_delay_applied(self) -> None:
        """Verify retries actually wait — uses asyncio.sleep, not wallclock."""
        chunk = StreamChunk(kind=ChunkKind.TEXT_DELTA, text="ok")
        backend = _ScriptedBackend(
            [
                [ConnectionError("a")],
                [chunk],
            ]
        )
        sleeps: list[float] = []
        original_sleep = asyncio.sleep

        async def mock_sleep(t: float) -> None:
            sleeps.append(t)
            await original_sleep(0)  # yield to event loop without real delay

        import unittest.mock

        with unittest.mock.patch("asyncio.sleep", mock_sleep):
            wrapped = RetryingBackend(backend, max_retries=3, base_delay_s=0.5)
            _ = [c async for c in wrapped.stream()]

        assert sleeps == [0.5]  # one retry, base_delay * 2^0 = 0.5
