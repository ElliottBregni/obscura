"""Tests for obscura.core.circuit_breaker."""

from __future__ import annotations

import time
from unittest.mock import patch

from obscura.core.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerRegistry,
    CircuitOpenError,
    CircuitState,
)


class TestCircuitBreakerInitial:
    def test_starts_closed(self) -> None:
        cb = CircuitBreaker("test")
        assert cb.state == CircuitState.CLOSED

    def test_name(self) -> None:
        cb = CircuitBreaker("claude")
        assert cb.name == "claude"

    def test_allows_requests_when_closed(self) -> None:
        cb = CircuitBreaker("test")
        assert cb.allow_request() is True


class TestCircuitBreakerTripping:
    def test_trips_after_threshold(self) -> None:
        cb = CircuitBreaker("test", failure_threshold=3)
        cb.record_failure()
        cb.record_failure()
        assert cb.state == CircuitState.CLOSED
        cb.record_failure()
        assert cb.state == CircuitState.OPEN

    def test_rejects_when_open(self) -> None:
        cb = CircuitBreaker("test", failure_threshold=1)
        cb.record_failure()
        assert cb.state == CircuitState.OPEN
        assert cb.allow_request() is False

    def test_failure_count(self) -> None:
        cb = CircuitBreaker("test", failure_threshold=5)
        for _ in range(3):
            cb.record_failure()
        assert cb.failure_count == 3


class TestCircuitBreakerRecovery:
    def test_success_resets_failure_count(self) -> None:
        cb = CircuitBreaker("test", failure_threshold=5)
        cb.record_failure()
        cb.record_failure()
        assert cb.failure_count == 2
        cb.record_success()
        assert cb.failure_count == 0
        assert cb.state == CircuitState.CLOSED

    def test_half_open_after_timeout(self) -> None:
        cb = CircuitBreaker("test", failure_threshold=1, recovery_timeout=0.01)
        cb.record_failure()
        assert cb.state == CircuitState.OPEN
        time.sleep(0.02)
        assert cb.state == CircuitState.HALF_OPEN

    def test_half_open_allows_limited_requests(self) -> None:
        cb = CircuitBreaker(
            "test", failure_threshold=1, recovery_timeout=0.01, half_open_max=1
        )
        cb.record_failure()
        time.sleep(0.02)
        assert cb.state == CircuitState.HALF_OPEN
        # First request allowed
        assert cb.allow_request() is True
        # Second blocked (half_open_max=1)
        assert cb.allow_request() is False

    def test_success_in_half_open_closes(self) -> None:
        cb = CircuitBreaker("test", failure_threshold=1, recovery_timeout=0.01)
        cb.record_failure()
        time.sleep(0.02)
        assert cb.state == CircuitState.HALF_OPEN
        cb.allow_request()
        cb.record_success()
        assert cb.state == CircuitState.CLOSED

    def test_failure_in_half_open_reopens(self) -> None:
        cb = CircuitBreaker("test", failure_threshold=1, recovery_timeout=0.01)
        cb.record_failure()
        time.sleep(0.02)
        assert cb.state == CircuitState.HALF_OPEN
        cb.allow_request()
        cb.record_failure()
        assert cb.state == CircuitState.OPEN


class TestCircuitBreakerReset:
    def test_reset_closes(self) -> None:
        cb = CircuitBreaker("test", failure_threshold=1)
        cb.record_failure()
        assert cb.state == CircuitState.OPEN
        cb.reset()
        assert cb.state == CircuitState.CLOSED
        assert cb.failure_count == 0
        assert cb.allow_request() is True


class TestCircuitBreakerTimeUntilHalfOpen:
    def test_zero_when_closed(self) -> None:
        cb = CircuitBreaker("test")
        assert cb.time_until_half_open() == 0.0

    def test_positive_when_open(self) -> None:
        cb = CircuitBreaker("test", failure_threshold=1, recovery_timeout=30.0)
        cb.record_failure()
        remaining = cb.time_until_half_open()
        assert remaining > 0.0
        assert remaining <= 30.0


class TestCircuitOpenError:
    def test_attributes(self) -> None:
        err = CircuitOpenError("claude", 5.0)
        assert err.name == "claude"
        assert err.retry_after == 5.0
        assert "claude" in str(err)


class TestCircuitBreakerRegistry:
    def test_get_creates_on_first_access(self) -> None:
        registry = CircuitBreakerRegistry()
        cb = registry.get("claude")
        assert cb.name == "claude"
        assert cb.state == CircuitState.CLOSED

    def test_get_returns_same_instance(self) -> None:
        registry = CircuitBreakerRegistry()
        cb1 = registry.get("claude")
        cb2 = registry.get("claude")
        assert cb1 is cb2

    def test_different_backends_different_breakers(self) -> None:
        registry = CircuitBreakerRegistry()
        cb1 = registry.get("claude")
        cb2 = registry.get("copilot")
        assert cb1 is not cb2

    def test_custom_defaults(self) -> None:
        registry = CircuitBreakerRegistry(
            failure_threshold=10, recovery_timeout=60.0
        )
        cb = registry.get("test")
        # Trip it to verify threshold
        for _ in range(9):
            cb.record_failure()
        assert cb.state == CircuitState.CLOSED
        cb.record_failure()
        assert cb.state == CircuitState.OPEN

    def test_all(self) -> None:
        registry = CircuitBreakerRegistry()
        registry.get("a")
        registry.get("b")
        all_breakers = registry.all()
        assert "a" in all_breakers
        assert "b" in all_breakers

    def test_reset_all(self) -> None:
        registry = CircuitBreakerRegistry(failure_threshold=1)
        cb1 = registry.get("a")
        cb2 = registry.get("b")
        cb1.record_failure()
        cb2.record_failure()
        assert cb1.state == CircuitState.OPEN
        assert cb2.state == CircuitState.OPEN
        registry.reset_all()
        assert cb1.state == CircuitState.CLOSED
        assert cb2.state == CircuitState.CLOSED
