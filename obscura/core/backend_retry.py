"""obscura.core.backend_retry — Wrap a BackendProtocol with retry on transient errors.

The agent loop itself shouldn't know about retry. Instead, wrap the backend
once at construction and the loop sees a transparent stream:

    backend = ClaudeBackend(...)
    backend = RetryingBackend(backend, max_retries=3)
    loop = AgentLoopV2(backend, registry)

Mirrors v1's ``with_retry`` helper that wrapped each ``backend.stream()``
call inline. v2 lifts this to the backend layer where it belongs.

Note: this retries only at the **stream level** — if a stream errors before
yielding any chunks, retry. Once chunks have been yielded, retry is unsafe
(the agent has already seen partial output and may have started executing
tools). Per-tool dedup via :class:`AgentLoopV2._seen_calls` covers the
mid-stream-retry case for caller-managed retries that DO restart after
partial output, but that's a separate concern.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from obscura.core.types import (
        BackendCapabilities,
        BackendProtocol,
        Message,
        StreamChunk,
    )


logger = logging.getLogger(__name__)


__all__ = ["RetryingBackend"]


# Default exception types we treat as transient. Caller can override.
_DEFAULT_TRANSIENT_EXC: tuple[type[BaseException], ...] = (
    ConnectionError,
    TimeoutError,
    asyncio.TimeoutError,  # noqa: UP041 — explicit alias for older code paths
)


class RetryingBackend:
    """Wraps a :class:`BackendProtocol` with retry-on-transient-error semantics.

    Only retries when the **first chunk** hasn't been yielded yet. Once any
    chunk has been emitted to the caller, retry is unsafe and the original
    exception propagates.

    Parameters
    ----------
    inner:
        The wrapped backend. Must implement ``BackendProtocol``.
    max_retries:
        Total attempts including the first. ``max_retries=3`` = try once,
        then up to two retries.
    base_delay_s:
        Base delay for exponential backoff. Each retry waits
        ``base_delay_s * (2 ** attempt)`` seconds.
    transient_exceptions:
        Exception types treated as transient. Default: ``ConnectionError``,
        ``TimeoutError``, ``asyncio.TimeoutError``.
    """

    def __init__(
        self,
        inner: BackendProtocol,
        *,
        max_retries: int = 3,
        base_delay_s: float = 0.5,
        transient_exceptions: tuple[type[BaseException], ...] | None = None,
    ) -> None:
        self._inner = inner
        self._max_retries = max(1, max_retries)
        self._base_delay = base_delay_s
        self._transient = transient_exceptions or _DEFAULT_TRANSIENT_EXC

    @property
    def name(self) -> str:
        return getattr(self._inner, "name", "retrying-backend")

    @property
    def capabilities(self) -> BackendCapabilities:
        return self._inner.capabilities

    async def start(self) -> None:
        await self._inner.start()

    async def close(self) -> None:
        await self._inner.close()

    async def stream(
        self,
        messages: list[Message] | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[StreamChunk]:
        """Stream from the inner backend, retrying on transient errors.

        Retry is attempted only before the first chunk is yielded. After
        that, errors propagate (re-streaming would double-emit content).
        """
        last_exc: BaseException | None = None
        for attempt in range(self._max_retries):
            yielded_any = False
            try:
                async for chunk in self._inner.stream(messages=messages, **kwargs):
                    yielded_any = True
                    yield chunk
                return
            except self._transient as exc:
                last_exc = exc
                if yielded_any:
                    # Mid-stream — retry would duplicate. Re-raise.
                    raise
                if attempt == self._max_retries - 1:
                    # Last attempt; let it propagate.
                    raise
                delay = self._base_delay * (2**attempt)
                logger.warning(
                    "RetryingBackend: %s on attempt %d/%d, retrying in %.2fs",
                    type(exc).__name__,
                    attempt + 1,
                    self._max_retries,
                    delay,
                )
                await asyncio.sleep(delay)
            except Exception:
                # Non-transient — don't retry.
                raise

        # Defensive — should be unreachable; the loop above either returns
        # or raises.
        if last_exc is not None:
            raise last_exc
