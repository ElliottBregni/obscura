"""Retry/backoff helper for transient vector-backend failures.

Bounded exponential backoff with jitter. Wraps the last exception in
:class:`VectorRetryExhausted` once the attempt budget is spent, so
callers see a uniform structured failure regardless of which underlying
client raised.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable

from obscura.data.vector_memory.errors import VectorRetryExhausted

logger = logging.getLogger(__name__)


def with_retry[T](
    op: str,
    func: Callable[[], T],
    *,
    attempts: int = 3,
    base_delay: float = 0.05,
    max_delay: float = 1.0,
    retry_on: tuple[type[Exception], ...] = (Exception,),
) -> T:
    """Run *func* up to *attempts* times with exponential backoff + jitter.

    Re-raises non-matching exceptions immediately so configuration
    errors don't get retried into a long timeout. The final failure is
    wrapped in :class:`VectorRetryExhausted` carrying the operation
    name, attempt count, and underlying exception.
    """
    last_exc: Exception | None = None
    for i in range(attempts):
        try:
            return func()
        except retry_on as exc:
            last_exc = exc
            if i == attempts - 1:
                break
            delay = min(base_delay * (2**i), max_delay)
            delay += random.uniform(0, delay * 0.25)
            logger.debug(
                "vector op %r attempt %d/%d failed (%s); retrying in %.3fs",
                op,
                i + 1,
                attempts,
                exc,
                delay,
            )
            time.sleep(delay)
    assert last_exc is not None  # noqa: S101
    raise VectorRetryExhausted(op, attempts, last_exc)
