"""
Tests for sdk.telemetry.metrics — Lazy metric instruments and ObscuraMetrics.

Verifies that all defined metrics are lazily created, fall back to NoOp
when OTel is unavailable, and record correctly with InMemoryMetricReader.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from sdk.telemetry.metrics import (
    ObscuraMetrics,
    _NoOpInstrument,
    get_metrics,
)


# ---------------------------------------------------------------------------
# NoOp fallback
# ---------------------------------------------------------------------------

class TestNoOpInstrument:
    def test_add_noop(self) -> None:
        inst = _NoOpInstrument()
        inst.add(1, {"key": "val"})  # should not raise

    def test_record_noop(self) -> None:
        inst = _NoOpInstrument()
        inst.record(0.5, {"key": "val"})  # should not raise

    def test_add_no_attributes(self) -> None:
        inst = _NoOpInstrument()
        inst.add(1)  # should not raise

    def test_record_no_attributes(self) -> None:
        inst = _NoOpInstrument()
        inst.record(0.5)  # should not raise


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

class TestGetMetrics:
    def test_returns_singleton(self) -> None:
        m1 = get_metrics()
        m2 = get_metrics()
        assert m1 is m2

    def test_returns_obscura_metrics(self) -> None:
        m = get_metrics()
        assert isinstance(m, ObscuraMetrics)


# ---------------------------------------------------------------------------
# Lazy metric attributes
# ---------------------------------------------------------------------------

class TestObscuraMetrics:
    def test_all_metrics_defined(self) -> None:
        """All expected metrics should be accessible."""
        m = get_metrics()
        # Counter metrics
        assert m.requests_total is not None
        assert m.agent_runs_total is not None
        assert m.tool_calls_total is not None
        assert m.sync_operations_total is not None
        assert m.stream_chunks_total is not None
        # Histogram metrics
        assert m.request_duration_seconds is not None
        assert m.agent_phase_duration_seconds is not None
        assert m.tool_duration_seconds is not None
        # UpDownCounter
        assert m.active_sessions is not None

    def test_noop_fallback_without_otel(self) -> None:
        """Without OTel, lazy metrics should return _NoOpInstrument."""
        with patch("sdk.telemetry.metrics._get_meter", return_value=None):
            # Create a fresh instance to avoid cached lazy attrs
            fresh = ObscuraMetrics()
            inst = fresh.requests_total
            assert isinstance(inst, _NoOpInstrument)

    def test_counter_add_is_callable(self) -> None:
        """Counter metrics should have an add() method."""
        m = get_metrics()
        assert callable(getattr(m.requests_total, "add", None))

    def test_histogram_record_is_callable(self) -> None:
        """Histogram metrics should have a record() method."""
        m = get_metrics()
        assert callable(getattr(m.request_duration_seconds, "record", None))

    def test_updowncounter_add_is_callable(self) -> None:
        """UpDownCounter should have an add() method."""
        m = get_metrics()
        assert callable(getattr(m.active_sessions, "add", None))


# ---------------------------------------------------------------------------
# InMemoryMetricReader (requires OTel SDK)
# ---------------------------------------------------------------------------

class TestMetricExport:
    """Tests that require opentelemetry-sdk installed."""

    @pytest.fixture(autouse=True)
    def _check_otel(self) -> None:
        try:
            import opentelemetry.sdk.metrics
        except ImportError:
            pytest.skip("opentelemetry-sdk not installed")

    def test_counter_records_value(self) -> None:
        """Counter.add() should produce a metric visible in InMemoryMetricReader."""
        from opentelemetry import metrics
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import InMemoryMetricReader

        reader = InMemoryMetricReader()
        provider = MeterProvider(metric_readers=[reader])
        metrics.set_meter_provider(provider)

        try:
            # Use the real meter
            meter = metrics.get_meter("test")
            counter = meter.create_counter("test_counter", description="test")
            counter.add(5, {"label": "x"})

            data = reader.get_metrics_data()
            assert data is not None
            # Verify we got metric data
            scopes = data.resource_metrics
            assert len(scopes) > 0
        finally:
            provider.shutdown()

    def test_histogram_records_value(self) -> None:
        """Histogram.record() should produce metric data."""
        from opentelemetry import metrics
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import InMemoryMetricReader

        reader = InMemoryMetricReader()
        provider = MeterProvider(metric_readers=[reader])
        
        # Try to set provider, but don't fail if already set
        try:
            metrics.set_meter_provider(provider)
        except Exception:
            # Provider already set, continue with testing the reader directly
            pass

        try:
            meter = provider.get_meter("test")
            hist = meter.create_histogram("test_histogram", unit="s")
            hist.record(0.5, {"op": "send"})

            data = reader.get_metrics_data()
            assert data is not None
            scopes = data.resource_metrics
            assert len(scopes) > 0
        finally:
            provider.shutdown()
