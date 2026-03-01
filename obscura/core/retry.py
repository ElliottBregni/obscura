"""obscura.core.retry — Retry with exponential backoff + circuit breaker.

Provides ``with_retry`` for wrapping async callables with configurable
retry logic, backoff, jitter, and optional circuit breaker integration.
"""

from __future__ import annotations

import asyncio
import random
from typing import Any, Awaitable, Callable, TypeVar

from obscura.core.circuit_breaker import CircuitBreaker, CircuitOpenError

T = TypeVar("T")


async def with_retry(
    fn: Callable[..., Awaitable[T]],
    *args: Any,
    max_retries: int = 2,
    initial_backoff: float = 0.5,
    max_backoff: float = 10.0,
    jitter: bool = True,
    circuit: CircuitBreaker | None = None,
    retryable: Callable[[Exception], bool] | None = None,
    **kwargs: Any,
) -> T:
    """Execute *fn* with exponential backoff + optional circuit breaker.

    Parameters
    ----------
    fn:
        Async callable to invoke.
    max_retries:
        Maximum number of retries (0 = no retries, just one attempt).
    initial_backoff:
        Seconds to wait after the first failure.
    max_backoff:
        Maximum backoff in seconds.
    jitter:
        Add random jitter to backoff (±25%).
    circuit:
        Optional :class:`CircuitBreaker` to integrate with.
    retryable:
        Optional predicate — ``(exc) -> bool``. If provided, only retry
        when it returns True. Defaults to retrying all exceptions.

    Raises
    ------
    CircuitOpenError
        If the circuit breaker is open.
    Exception
        The last exception from *fn* after all retries are exhausted.
    """
    last_exc: Exception | None = None
    attempts = max_retries + 1  # total attempts = retries + 1

    for attempt in range(attempts):
        # Check circuit breaker
        if circuit is not None and not circuit.allow_request():
            raise CircuitOpenError(
                circuit.name, circuit.time_until_half_open()
            )

        try:
            result = await fn(*args, **kwargs)
            # Success
            if circuit is not None:
                circuit.record_success()
            return result
        except Exception as exc:
            last_exc = exc

            # Record failure in circuit breaker
            if circuit is not None:
                circuit.record_failure()

            # Check if this exception is retryable
            if retryable is not None and not retryable(exc):
                raise

            # Last attempt — don't sleep, just raise
            if attempt == attempts - 1:
                break

            # Record retry metric
            _record_retry(attempt + 1)

            # Exponential backoff
            backoff = min(initial_backoff * (2 ** attempt), max_backoff)
            if jitter:
                backoff *= 0.75 + random.random() * 0.5  # ±25%

            await asyncio.sleep(backoff)

    assert last_exc is not None  # make pyright happy
    raise last_exc


def _record_retry(attempt: int) -> None:
    """Emit a retry metric."""
    try:
        from obscura.telemetry.metrics import get_metrics

        get_metrics().retry_attempts.add(1, {"attempt": str(attempt)})
    except Exception:
        pass
