"""Tests for obscura.core.retry."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from obscura.core.circuit_breaker import CircuitBreaker, CircuitOpenError
from obscura.core.retry import with_retry


@pytest.mark.asyncio
class TestWithRetryBasic:
    async def test_success_no_retry(self) -> None:
        fn = AsyncMock(return_value="ok")
        result = await with_retry(fn, max_retries=2, initial_backoff=0.0)
        assert result == "ok"
        assert fn.call_count == 1

    async def test_fails_then_succeeds(self) -> None:
        fn = AsyncMock(side_effect=[ValueError("boom"), "ok"])
        result = await with_retry(fn, max_retries=1, initial_backoff=0.0)
        assert result == "ok"
        assert fn.call_count == 2

    async def test_all_retries_exhausted(self) -> None:
        fn = AsyncMock(side_effect=ValueError("nope"))
        with pytest.raises(ValueError, match="nope"):
            await with_retry(fn, max_retries=2, initial_backoff=0.0)
        assert fn.call_count == 3  # 1 + 2 retries

    async def test_no_retries(self) -> None:
        fn = AsyncMock(side_effect=RuntimeError("fail"))
        with pytest.raises(RuntimeError):
            await with_retry(fn, max_retries=0, initial_backoff=0.0)
        assert fn.call_count == 1

    async def test_passes_args_and_kwargs(self) -> None:
        fn = AsyncMock(return_value="done")
        result = await with_retry(fn, "a", "b", key="val", max_retries=0)
        fn.assert_called_once_with("a", "b", key="val")
        assert result == "done"


@pytest.mark.asyncio
class TestWithRetryRetryable:
    async def test_retryable_true(self) -> None:
        fn = AsyncMock(side_effect=[ValueError("retry"), "ok"])
        result = await with_retry(
            fn,
            max_retries=1,
            initial_backoff=0.0,
            retryable=lambda exc: isinstance(exc, ValueError),
        )
        assert result == "ok"

    async def test_retryable_false_raises_immediately(self) -> None:
        fn = AsyncMock(side_effect=TypeError("nope"))
        with pytest.raises(TypeError):
            await with_retry(
                fn,
                max_retries=3,
                initial_backoff=0.0,
                retryable=lambda exc: isinstance(exc, ValueError),
            )
        assert fn.call_count == 1


@pytest.mark.asyncio
class TestWithRetryCircuitBreaker:
    async def test_circuit_open_raises(self) -> None:
        cb = CircuitBreaker("test", failure_threshold=1)
        cb.record_failure()  # trips to OPEN
        fn = AsyncMock(return_value="ok")
        with pytest.raises(CircuitOpenError):
            await with_retry(fn, circuit=cb, max_retries=0)
        assert fn.call_count == 0  # never called

    async def test_circuit_records_success(self) -> None:
        cb = CircuitBreaker("test", failure_threshold=5)
        cb.record_failure()
        cb.record_failure()
        fn = AsyncMock(return_value="ok")
        await with_retry(fn, circuit=cb, max_retries=0)
        assert cb.failure_count == 0  # reset by success

    async def test_circuit_records_failure(self) -> None:
        cb = CircuitBreaker("test", failure_threshold=5)
        fn = AsyncMock(side_effect=RuntimeError("fail"))
        with pytest.raises(RuntimeError):
            await with_retry(fn, circuit=cb, max_retries=0, initial_backoff=0.0)
        assert cb.failure_count == 1


@pytest.mark.asyncio
class TestWithRetryBackoff:
    async def test_backoff_called(self) -> None:
        fn = AsyncMock(side_effect=[ValueError("1"), ValueError("2"), "ok"])
        with patch("obscura.core.retry.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            result = await with_retry(
                fn, max_retries=2, initial_backoff=1.0, jitter=False
            )
        assert result == "ok"
        # Two sleeps: after attempt 0 and attempt 1
        assert mock_sleep.call_count == 2
        # First backoff: 1.0 * 2^0 = 1.0
        first_backoff = mock_sleep.call_args_list[0][0][0]
        assert first_backoff == pytest.approx(1.0)
        # Second backoff: 1.0 * 2^1 = 2.0
        second_backoff = mock_sleep.call_args_list[1][0][0]
        assert second_backoff == pytest.approx(2.0)

    async def test_max_backoff_capped(self) -> None:
        fn = AsyncMock(side_effect=[ValueError("1"), ValueError("2"), "ok"])
        with patch("obscura.core.retry.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            result = await with_retry(
                fn, max_retries=2, initial_backoff=5.0, max_backoff=3.0, jitter=False
            )
        assert result == "ok"
        for call in mock_sleep.call_args_list:
            assert call[0][0] <= 3.0

    async def test_jitter_varies_backoff(self) -> None:
        fn = AsyncMock(side_effect=[ValueError("1"), ValueError("2"), "ok"])
        backoffs: list[float] = []
        original_sleep = asyncio.sleep

        async def capture_sleep(t: float) -> None:
            backoffs.append(t)

        with patch("obscura.core.retry.asyncio.sleep", side_effect=capture_sleep):
            await with_retry(
                fn, max_retries=2, initial_backoff=1.0, jitter=True
            )
        # With jitter, backoff should be in range [0.75, 1.25] * base
        assert 0.5 <= backoffs[0] <= 1.5
