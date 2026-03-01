"""Tests for obscura.core.rate_limiter."""

from __future__ import annotations

import time

from obscura.core.rate_limiter import RateLimiter, RateLimitResult


class TestRateLimitResult:
    def test_frozen(self) -> None:
        r = RateLimitResult(allowed=True, remaining=5, limit=10)
        assert r.allowed is True
        assert r.remaining == 5
        assert r.retry_after_seconds == 0.0

    def test_denied(self) -> None:
        r = RateLimitResult(allowed=False, remaining=0, retry_after_seconds=5.0, limit=100)
        assert r.allowed is False
        assert r.retry_after_seconds == 5.0


class TestRateLimiterDefaults:
    def test_default_limits(self) -> None:
        limiter = RateLimiter()
        limits = limiter.get_limits("user1")
        assert limits["rpm"] == 100
        assert limits["concurrent"] == 10

    def test_custom_defaults(self) -> None:
        limiter = RateLimiter(default_rpm=50, default_concurrent=5)
        limits = limiter.get_limits("user1")
        assert limits["rpm"] == 50
        assert limits["concurrent"] == 5


class TestRateLimiterAcquire:
    def test_acquire_allowed(self) -> None:
        limiter = RateLimiter(default_rpm=10)
        result = limiter.acquire("u1")
        assert result.allowed is True
        assert result.remaining == 9
        assert result.limit == 10

    def test_acquire_decrements_remaining(self) -> None:
        limiter = RateLimiter(default_rpm=3)
        r1 = limiter.acquire("u1")
        r2 = limiter.acquire("u1")
        r3 = limiter.acquire("u1")
        assert r1.remaining == 2
        assert r2.remaining == 1
        assert r3.remaining == 0

    def test_acquire_denied_at_limit(self) -> None:
        limiter = RateLimiter(default_rpm=2)
        limiter.acquire("u1")
        limiter.acquire("u1")
        result = limiter.acquire("u1")
        assert result.allowed is False
        assert result.remaining == 0
        assert result.retry_after_seconds > 0

    def test_acquire_independent_users(self) -> None:
        limiter = RateLimiter(default_rpm=1)
        r1 = limiter.acquire("alice")
        r2 = limiter.acquire("bob")
        assert r1.allowed is True
        assert r2.allowed is True


class TestRateLimiterConcurrent:
    def test_concurrent_limit(self) -> None:
        limiter = RateLimiter(default_rpm=100, default_concurrent=2)
        r1 = limiter.acquire("u1")
        r2 = limiter.acquire("u1")
        assert r1.allowed is True
        assert r2.allowed is True
        r3 = limiter.acquire("u1")
        assert r3.allowed is False

    def test_release_concurrent(self) -> None:
        limiter = RateLimiter(default_rpm=100, default_concurrent=1)
        r1 = limiter.acquire("u1")
        assert r1.allowed is True
        r2 = limiter.acquire("u1")
        assert r2.allowed is False
        limiter.release_concurrent("u1")
        r3 = limiter.acquire("u1")
        assert r3.allowed is True

    def test_release_no_negative(self) -> None:
        limiter = RateLimiter()
        limiter.release_concurrent("u1")  # should not go negative
        # Acquire should still work fine
        result = limiter.acquire("u1")
        assert result.allowed is True


class TestRateLimiterCheck:
    def test_check_does_not_consume(self) -> None:
        limiter = RateLimiter(default_rpm=1)
        c1 = limiter.check("u1")
        c2 = limiter.check("u1")
        assert c1.allowed is True
        assert c2.allowed is True
        # Now acquire should still work
        r = limiter.acquire("u1")
        assert r.allowed is True

    def test_check_reflects_state(self) -> None:
        limiter = RateLimiter(default_rpm=1)
        limiter.acquire("u1")
        c = limiter.check("u1")
        assert c.allowed is False


class TestRateLimiterCustomLimits:
    def test_set_custom_rpm(self) -> None:
        limiter = RateLimiter(default_rpm=100)
        limiter.set_limits("vip", rpm=500)
        limits = limiter.get_limits("vip")
        assert limits["rpm"] == 500
        assert limits["concurrent"] == 10  # still default

    def test_set_custom_concurrent(self) -> None:
        limiter = RateLimiter()
        limiter.set_limits("vip", concurrent=50)
        limits = limiter.get_limits("vip")
        assert limits["concurrent"] == 50
        assert limits["rpm"] == 100  # still default

    def test_set_both(self) -> None:
        limiter = RateLimiter()
        limiter.set_limits("vip", rpm=200, concurrent=20)
        limits = limiter.get_limits("vip")
        assert limits["rpm"] == 200
        assert limits["concurrent"] == 20

    def test_custom_limits_enforced(self) -> None:
        limiter = RateLimiter(default_rpm=100)
        limiter.set_limits("restricted", rpm=1)
        limiter.acquire("restricted")
        r = limiter.acquire("restricted")
        assert r.allowed is False


class TestRateLimiterSlidingWindow:
    def test_window_expires(self) -> None:
        limiter = RateLimiter(default_rpm=1)
        # Manually inject an old timestamp
        window = limiter._get_window("u1")
        window.append(time.monotonic() - 61.0)  # 61s ago = expired
        limiter._concurrent["u1"] = 0
        result = limiter.acquire("u1")
        assert result.allowed is True


class TestRateLimiterClear:
    def test_clear_resets_all(self) -> None:
        limiter = RateLimiter(default_rpm=5)
        limiter.acquire("u1")
        limiter.set_limits("u1", rpm=500)
        limiter.clear()
        # After clear, custom limits removed, history cleared
        limits = limiter.get_limits("u1")
        assert limits["rpm"] == 5  # back to constructor default
        result = limiter.acquire("u1")
        assert result.allowed is True
