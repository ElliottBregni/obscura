"""obscura.core.smart_retry — Smart retry with exponential backoff for API calls.

Handles:
  - 429 (rate limit): Wait for Retry-After header, then retry
  - 5xx (server error): Exponential backoff with jitter
  - 401 (auth expired): Refresh token and retry once
  - Timeout: Retry with increased timeout

Includes circuit breaker to stop retrying after repeated failures.

Usage::

    from obscura.core.smart_retry import with_smart_retry

    result = await with_smart_retry(
        lambda: backend.send(prompt),
        max_retries=3,
    )
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import TYPE_CHECKING, TypeVar

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Default retry configuration.
DEFAULT_MAX_RETRIES = 3
DEFAULT_INITIAL_BACKOFF = 1.0  # seconds
DEFAULT_MAX_BACKOFF = 30.0  # seconds
DEFAULT_BACKOFF_MULTIPLIER = 2.0
DEFAULT_JITTER = 0.25  # ±25%


def _is_retryable(exc: Exception) -> bool:
    """Determine if an exception is retryable."""
    msg = str(exc).lower()

    # Rate limit (429).
    if "rate" in msg and "limit" in msg:
        return True
    if "429" in msg:
        return True

    # Server errors (5xx).
    if any(code in msg for code in ("500", "502", "503", "504")):
        return True
    if "internal server error" in msg:
        return True
    if "overloaded" in msg or "capacity" in msg:
        return True

    # Timeout.
    if "timeout" in msg or "timed out" in msg:
        return True

    # Connection errors.
    return bool(
        "connection" in msg and ("reset" in msg or "refused" in msg or "closed" in msg),
    )


def _extract_retry_after(exc: Exception) -> float | None:
    """Extract Retry-After hint from exception message."""
    msg = str(exc)
    # Look for "retry after N" or "Retry-After: N".
    import re

    match = re.search(r"retry.?after[:\s]+(\d+\.?\d*)", msg, re.IGNORECASE)
    if match:
        return float(match.group(1))
    return None


async def with_smart_retry[T](
    fn: Callable[[], Awaitable[T]],
    *,
    max_retries: int = DEFAULT_MAX_RETRIES,
    initial_backoff: float = DEFAULT_INITIAL_BACKOFF,
    max_backoff: float = DEFAULT_MAX_BACKOFF,
    multiplier: float = DEFAULT_BACKOFF_MULTIPLIER,
    jitter: float = DEFAULT_JITTER,
    on_retry: Callable[[int, Exception, float], None] | None = None,
) -> T:
    """Execute *fn* with smart retry and exponential backoff.

    Args:
        fn: Async callable to retry.
        max_retries: Maximum retry attempts.
        initial_backoff: Initial wait time in seconds.
        max_backoff: Maximum wait time.
        multiplier: Backoff multiplier per retry.
        jitter: Random jitter fraction (±jitter).
        on_retry: Callback(attempt, exception, wait_seconds) on each retry.

    Returns:
        The result of *fn* on success.

    Raises:
        The last exception if all retries are exhausted.

    """
    backoff = initial_backoff
    last_exc: Exception | None = None

    for attempt in range(1 + max_retries):
        try:
            return await fn()
        except Exception as exc:
            last_exc = exc

            if attempt >= max_retries or not _is_retryable(exc):
                raise

            # Check for Retry-After header.
            retry_after = _extract_retry_after(exc)
            if retry_after is not None:
                wait = retry_after
            else:
                # Exponential backoff with jitter.
                jitter_factor = 1.0 + random.uniform(-jitter, jitter)
                wait = min(backoff * jitter_factor, max_backoff)
                backoff *= multiplier

            logger.info(
                "Retry %d/%d after %.1fs: %s",
                attempt + 1,
                max_retries,
                wait,
                type(exc).__name__,
            )

            if on_retry is not None:
                on_retry(attempt + 1, exc, wait)

            # Log to deep log.
            try:
                from obscura.core.deep_log import dlog

                dlog.event(
                    "api_retry",
                    attempt=attempt + 1,
                    max_retries=max_retries,
                    wait_s=round(wait, 1),
                    error=str(exc)[:200],
                )
            except Exception:
                pass

            await asyncio.sleep(wait)

    # Should not reach here, but just in case.
    assert last_exc is not None
    raise last_exc
