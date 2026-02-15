"""Tests for sdk.telemetry.traces — get_tracer, traced decorator, NoOp classes."""

import pytest
from unittest.mock import patch

from sdk.telemetry.traces import (
    get_tracer,
    set_span_attribute,
    set_span_status_error,
    record_exception,
    traced,
    NoOpSpan,
    NoOpTracer,
)


class TestNoOpSpan:
    def test_set_attribute(self):
        span = NoOpSpan()
        span.set_attribute("key", "value")  # Should not raise

    def test_set_status(self):
        span = NoOpSpan()
        span.set_status("ERROR", "desc")

    def test_record_exception(self):
        span = NoOpSpan()
        span.record_exception(RuntimeError("oops"))

    def test_is_recording(self):
        span = NoOpSpan()
        assert span.is_recording() is False

    def test_context_manager(self):
        span = NoOpSpan()
        with span as s:
            assert s is span


class TestNoOpTracer:
    def test_start_as_current_span(self):
        tracer = NoOpTracer()
        span = tracer.start_as_current_span("test")
        assert isinstance(span, NoOpSpan)

    def test_start_span(self):
        tracer = NoOpTracer()
        span = tracer.start_span("test")
        assert isinstance(span, NoOpSpan)


class TestGetTracer:
    def test_returns_noop_without_otel(self):
        with patch.dict("sys.modules", {"opentelemetry": None}):
            tracer = get_tracer("test")
            assert isinstance(tracer, NoOpTracer)


class TestSetSpanAttribute:
    def test_no_otel(self):
        with patch.dict("sys.modules", {"opentelemetry": None}):
            set_span_attribute("key", "value")  # Should not raise


class TestSetSpanStatusError:
    def test_no_otel(self):
        with patch.dict("sys.modules", {"opentelemetry": None}):
            set_span_status_error("error desc")  # Should not raise


class TestRecordException:
    def test_no_otel(self):
        with patch.dict("sys.modules", {"opentelemetry": None}):
            record_exception(RuntimeError("test"))  # Should not raise


class TestTracedDecorator:
    def test_sync_function(self):
        @traced("test_op")
        def my_func(x: int) -> int:
            return x * 2

        result = my_func(5)
        assert result == 10

    @pytest.mark.asyncio
    async def test_async_function(self):
        @traced("test_async_op")
        async def my_func(x: int) -> int:
            return x * 2

        result = await my_func(5)
        assert result == 10

    def test_sync_function_default_name(self):
        @traced()
        def another_func() -> str:
            return "ok"

        result = another_func()
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_async_function_default_name(self):
        @traced()
        async def another_async() -> str:
            return "ok"

        result = await another_async()
        assert result == "ok"

    def test_sync_raises(self):
        @traced("failing_op")
        def failing():
            raise ValueError("fail")

        with pytest.raises(ValueError, match="fail"):
            failing()

    @pytest.mark.asyncio
    async def test_async_raises(self):
        @traced("async_failing")
        async def async_failing():
            raise ValueError("fail")

        with pytest.raises(ValueError, match="fail"):
            await async_failing()

    def test_with_attributes(self):
        @traced("op", attributes={"key": "value"})
        def my_fn():
            return "ok"

        assert my_fn() == "ok"
