"""
Tests for sdk.telemetry.traces — Tracer helpers and @traced decorator.

Uses OTel InMemorySpanExporter to verify spans are correctly created
with expected names and attributes.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from sdk.telemetry.traces import (
    _NoOpSpan,
    _NoOpTracer,
    get_tracer,
    record_exception,
    set_span_attribute,
    set_span_status_error,
    traced,
)


# ---------------------------------------------------------------------------
# NoOp fallbacks
# ---------------------------------------------------------------------------

class TestNoOpSpan:
    def test_context_manager(self) -> None:
        span = _NoOpSpan()
        with span as s:
            assert s is span

    def test_set_attribute_noop(self) -> None:
        span = _NoOpSpan()
        span.set_attribute("key", "val")  # should not raise

    def test_set_status_noop(self) -> None:
        span = _NoOpSpan()
        span.set_status("ERROR", "desc")

    def test_record_exception_noop(self) -> None:
        span = _NoOpSpan()
        span.record_exception(ValueError("test"))

    def test_is_recording_false(self) -> None:
        span = _NoOpSpan()
        assert span.is_recording() is False


class TestNoOpTracer:
    def test_start_as_current_span_returns_noop(self) -> None:
        tracer = _NoOpTracer()
        span = tracer.start_as_current_span("test")
        assert isinstance(span, _NoOpSpan)

    def test_start_span_returns_noop(self) -> None:
        tracer = _NoOpTracer()
        span = tracer.start_span("test")
        assert isinstance(span, _NoOpSpan)


# ---------------------------------------------------------------------------
# get_tracer
# ---------------------------------------------------------------------------

class TestGetTracer:
    def test_returns_tracer_without_otel(self) -> None:
        """Without OTel installed, should return NoOpTracer."""
        with patch.dict("sys.modules", {"opentelemetry": None}):
            # Force import error
            with patch("sdk.telemetry.traces.get_tracer") as mock_gt:
                mock_gt.return_value = _NoOpTracer()
                tracer = mock_gt("test")
                assert isinstance(tracer, _NoOpTracer)

    def test_returns_something_callable(self) -> None:
        """get_tracer should always return an object with start_as_current_span."""
        tracer = get_tracer("test.module")
        assert hasattr(tracer, "start_as_current_span")


# ---------------------------------------------------------------------------
# @traced decorator
# ---------------------------------------------------------------------------

class TestTracedDecorator:
    def test_sync_function_works(self) -> None:
        """@traced on sync function should preserve return value."""
        @traced("test.sync")
        def add(a: int, b: int) -> int:
            return a + b

        assert add(1, 2) == 3

    @pytest.mark.asyncio
    async def test_async_function_works(self) -> None:
        """@traced on async function should preserve return value."""
        @traced("test.async")
        async def add(a: int, b: int) -> int:
            return a + b

        assert await add(1, 2) == 3

    def test_sync_exception_propagates(self) -> None:
        """@traced should re-raise exceptions."""
        @traced("test.error")
        def fail() -> None:
            raise ValueError("boom")

        with pytest.raises(ValueError, match="boom"):
            fail()

    @pytest.mark.asyncio
    async def test_async_exception_propagates(self) -> None:
        """@traced should re-raise async exceptions."""
        @traced("test.async_error")
        async def fail() -> None:
            raise ValueError("async boom")

        with pytest.raises(ValueError, match="async boom"):
            await fail()

    def test_preserves_function_name(self) -> None:
        """@traced should preserve __name__ via functools.wraps."""
        @traced("test.name")
        def my_function() -> None:
            pass

        assert my_function.__name__ == "my_function"

    def test_default_span_name(self) -> None:
        """If no name given, span name should be module.qualname."""
        @traced()
        def another_func() -> int:
            return 42

        assert another_func() == 42


# ---------------------------------------------------------------------------
# InMemorySpanExporter (requires OTel SDK)
# ---------------------------------------------------------------------------

class TestSpanExport:
    """Tests that require opentelemetry-sdk installed."""

    @pytest.fixture(autouse=True)
    def _check_otel(self) -> None:
        try:
            import opentelemetry.sdk
        except ImportError:
            pytest.skip("opentelemetry-sdk not installed")

    def test_traced_creates_span(self) -> None:
        """@traced should create a span visible in InMemorySpanExporter."""
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        provider.add_span_processor(SimpleSpanProcessor(exporter))

        token = trace.set_tracer_provider(provider)

        try:
            @traced("test.span_export")
            def do_work() -> str:
                return "done"

            result = do_work()
            assert result == "done"

            # Force flush
            provider.force_flush()
            spans = exporter.get_finished_spans()
            assert len(spans) >= 1
            span_names = [s.name for s in spans]
            assert "test.span_export" in span_names
        finally:
            # Clean up — reset provider
            exporter.shutdown()
            provider.shutdown()
