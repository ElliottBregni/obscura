"""obscura.core.rate_limiter — In-memory sliding-window rate limiter.

Tracks per-user request rates and concurrent request counts.
Designed for single-process deployment; for multi-process, swap
to Redis-backed storage.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True)
class RateLimitResult:
    """Outcome of a rate limit check."""

    allowed: bool
    remaining: int
    retry_after_seconds: float = 0.0
    limit: int = 0


class RateLimiter:
    """In-memory sliding-window rate limiter keyed by ``user_id``.

    Each user has:
    - A sliding window of request timestamps (60-second window)
    - A concurrent request counter

    Custom per-user limits can be set via :meth:`set_limits`.
    """

    def __init__(
        self,
        default_rpm: int = 100,
        default_concurrent: int = 10,
    ) -> None:
        self._default_rpm = default_rpm
        self._default_concurrent = default_concurrent
        self._windows: dict[str, deque[float]] = {}
        self._concurrent: dict[str, int] = {}
        self._custom_limits: dict[str, dict[str, int]] = {}

    # -- Public API ----------------------------------------------------------

    def check(self, user_id: str) -> RateLimitResult:
        """Check if a request would be allowed without consuming a slot."""
        limits = self.get_limits(user_id)
        rpm = limits["rpm"]
        concurrent = limits["concurrent"]

        now = time.monotonic()
        window = self._get_window(user_id)
        self._prune_window(window, now)

        active = self._concurrent.get(user_id, 0)

        if active >= concurrent:
            return RateLimitResult(
                allowed=False,
                remaining=0,
                retry_after_seconds=1.0,
                limit=concurrent,
            )

        count = len(window)
        if count >= rpm:
            oldest = window[0]
            retry_after = 60.0 - (now - oldest)
            return RateLimitResult(
                allowed=False,
                remaining=0,
                retry_after_seconds=max(retry_after, 0.1),
                limit=rpm,
            )

        return RateLimitResult(
            allowed=True,
            remaining=rpm - count,
            limit=rpm,
        )

    def acquire(self, user_id: str) -> RateLimitResult:
        """Consume a rate-limit slot and increment concurrent count.

        Returns ``allowed=False`` with ``retry_after_seconds`` if the user
        has exceeded their limit.
        """
        result = self.check(user_id)
        if not result.allowed:
            return result

        now = time.monotonic()
        window = self._get_window(user_id)
        window.append(now)
        self._concurrent[user_id] = self._concurrent.get(user_id, 0) + 1

        limits = self.get_limits(user_id)
        remaining = limits["rpm"] - len(window)
        return RateLimitResult(
            allowed=True,
            remaining=max(remaining, 0),
            limit=limits["rpm"],
        )

    def release_concurrent(self, user_id: str) -> None:
        """Release a concurrent slot after a request completes."""
        current = self._concurrent.get(user_id, 0)
        if current > 0:
            self._concurrent[user_id] = current - 1

    def set_limits(
        self,
        user_id: str,
        rpm: int | None = None,
        concurrent: int | None = None,
    ) -> None:
        """Set custom per-user limits."""
        existing = self._custom_limits.get(user_id, {})
        if rpm is not None:
            existing["rpm"] = rpm
        if concurrent is not None:
            existing["concurrent"] = concurrent
        self._custom_limits[user_id] = existing

    def get_limits(self, user_id: str) -> dict[str, int]:
        """Return effective limits for a user."""
        custom = self._custom_limits.get(user_id, {})
        return {
            "rpm": custom.get("rpm", self._default_rpm),
            "concurrent": custom.get("concurrent", self._default_concurrent),
        }

    def clear(self) -> None:
        """Reset all state. Used by test fixtures."""
        self._windows.clear()
        self._concurrent.clear()
        self._custom_limits.clear()

    # -- Internal helpers ----------------------------------------------------

    def _get_window(self, user_id: str) -> deque[float]:
        if user_id not in self._windows:
            self._windows[user_id] = deque()
        return self._windows[user_id]

    @staticmethod
    def _prune_window(window: deque[float], now: float) -> None:
        """Remove timestamps older than 60 seconds."""
        cutoff = now - 60.0
        while window and window[0] < cutoff:
            window.popleft()
