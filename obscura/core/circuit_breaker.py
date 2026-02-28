"""obscura.core.circuit_breaker — Per-backend circuit breaker.

Classic three-state pattern (CLOSED → OPEN → HALF_OPEN) that prevents
cascading failures when an LLM backend goes down.
"""

from __future__ import annotations

import enum
import threading
import time
from typing import Any


class CircuitState(enum.Enum):
    """State of a circuit breaker."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(Exception):
    """Raised when a request is rejected because the circuit is open."""

    def __init__(self, name: str, retry_after: float) -> None:
        self.name = name
        self.retry_after = retry_after
        super().__init__(
            f"Circuit breaker '{name}' is open; retry after {retry_after:.1f}s"
        )


class CircuitBreaker:
    """Per-backend circuit breaker with configurable thresholds.

    Parameters
    ----------
    name:
        Identifier for this breaker (typically the backend name).
    failure_threshold:
        Number of consecutive failures before the circuit opens.
    recovery_timeout:
        Seconds to wait in OPEN state before transitioning to HALF_OPEN.
    half_open_max:
        Maximum concurrent requests allowed in HALF_OPEN state.
    """

    def __init__(
        self,
        name: str,
        *,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        half_open_max: int = 1,
    ) -> None:
        self._name = name
        self._failure_threshold = failure_threshold
        self._recovery_timeout = recovery_timeout
        self._half_open_max = half_open_max

        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._last_failure_time = 0.0
        self._half_open_in_flight = 0
        self._lock = threading.Lock()

    @property
    def name(self) -> str:
        return self._name

    @property
    def state(self) -> CircuitState:
        with self._lock:
            self._maybe_transition_to_half_open()
            return self._state

    @property
    def failure_count(self) -> int:
        return self._failure_count

    def allow_request(self) -> bool:
        """Check if a request should be allowed through.

        Returns True if allowed, False if rejected.
        """
        with self._lock:
            self._maybe_transition_to_half_open()

            if self._state == CircuitState.CLOSED:
                return True

            if self._state == CircuitState.HALF_OPEN:
                if self._half_open_in_flight < self._half_open_max:
                    self._half_open_in_flight += 1
                    return True
                return False

            # OPEN
            return False

    def record_success(self) -> None:
        """Record a successful call. Resets failure count / closes circuit."""
        with self._lock:
            if self._state == CircuitState.HALF_OPEN:
                self._half_open_in_flight = max(0, self._half_open_in_flight - 1)
            self._failure_count = 0
            self._state = CircuitState.CLOSED

    def record_failure(self) -> None:
        """Record a failed call. May trip to OPEN."""
        with self._lock:
            if self._state == CircuitState.HALF_OPEN:
                self._half_open_in_flight = max(0, self._half_open_in_flight - 1)

            self._failure_count += 1
            self._last_failure_time = time.monotonic()

            if self._failure_count >= self._failure_threshold:
                if self._state != CircuitState.OPEN:
                    self._state = CircuitState.OPEN
                    self._record_trip()

    def reset(self) -> None:
        """Force reset to CLOSED (admin/testing)."""
        with self._lock:
            self._state = CircuitState.CLOSED
            self._failure_count = 0
            self._half_open_in_flight = 0

    def time_until_half_open(self) -> float:
        """Seconds remaining until the circuit transitions to HALF_OPEN."""
        with self._lock:
            if self._state != CircuitState.OPEN:
                return 0.0
            elapsed = time.monotonic() - self._last_failure_time
            remaining = self._recovery_timeout - elapsed
            return max(remaining, 0.0)

    # -- Internal helpers ----------------------------------------------------

    def _maybe_transition_to_half_open(self) -> None:
        """Check if enough time has passed to move from OPEN → HALF_OPEN."""
        if self._state != CircuitState.OPEN:
            return
        elapsed = time.monotonic() - self._last_failure_time
        if elapsed >= self._recovery_timeout:
            self._state = CircuitState.HALF_OPEN
            self._half_open_in_flight = 0

    def _record_trip(self) -> None:
        """Emit a circuit-breaker trip metric."""
        try:
            from obscura.telemetry.metrics import get_metrics

            get_metrics().circuit_breaker_trips.add(
                1, {"backend": self._name, "to_state": "open"}
            )
        except Exception:
            pass


class CircuitBreakerRegistry:
    """Maps backend names to their circuit breakers.

    Lazily creates breakers on first access with configurable defaults.
    """

    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        half_open_max: int = 1,
    ) -> None:
        self._defaults = {
            "failure_threshold": failure_threshold,
            "recovery_timeout": recovery_timeout,
            "half_open_max": half_open_max,
        }
        self._breakers: dict[str, CircuitBreaker] = {}
        self._lock = threading.Lock()

    def get(self, backend: str) -> CircuitBreaker:
        """Get or create a circuit breaker for a backend."""
        with self._lock:
            if backend not in self._breakers:
                self._breakers[backend] = CircuitBreaker(
                    backend, **self._defaults
                )
            return self._breakers[backend]

    def all(self) -> dict[str, CircuitBreaker]:
        """Return all registered breakers."""
        with self._lock:
            return dict(self._breakers)

    def reset_all(self) -> None:
        """Reset all breakers to CLOSED."""
        with self._lock:
            for breaker in self._breakers.values():
                breaker.reset()
