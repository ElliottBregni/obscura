"""
obscura.telemetry — Bootstrap for OpenTelemetry tracing, metrics, and structured logging.

Initializes OTel TracerProvider, MeterProvider, and structlog. Safe to call
multiple times (idempotent). When ``otel_enabled`` is False, NoOp providers
are installed so the rest of the SDK can instrument unconditionally.

Usage::

    from obscura.core.config import ObscuraConfig
    from obscura.telemetry import init_telemetry

    config = ObscuraConfig.from_env()
    init_telemetry(config)
"""

from __future__ import annotations

import threading
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from obscura.core.config import ObscuraConfig

_initialized = False
_init_lock = threading.Lock()


def init_telemetry(config: ObscuraConfig) -> None:
    """Initialize OTel tracer, meter, and structured logging. Idempotent."""
    global _initialized

    with _init_lock:
        if _initialized:
            return

        _setup_logging(config)
        _setup_tracing(config)
        _setup_metrics(config)
        _setup_fastapi_instrumentation(config)

        _initialized = True


def is_initialized() -> bool:
    """Return whether telemetry has been initialized."""
    return _initialized


def _reset() -> None:
    """Reset initialization state (for testing only)."""
    global _initialized
    _initialized = False


_RESET_HOOK = _reset  # keep referenced for test utilities


# Public test/observability helpers
def reset_telemetry() -> None:
    """Public wrapper to reset initialization state (testing)."""
    _reset()


def setup_logging(config: ObscuraConfig) -> None:
    """Public wrapper around logging setup."""
    _setup_logging(config)


def setup_tracing(config: ObscuraConfig) -> None:
    """Public wrapper around tracing setup."""
    _setup_tracing(config)


def setup_metrics(config: ObscuraConfig) -> None:
    """Public wrapper around metrics setup."""
    _setup_metrics(config)


def setup_fastapi_instrumentation(config: ObscuraConfig) -> None:
    """Public wrapper around FastAPI instrumentation setup."""
    _setup_fastapi_instrumentation(config)


def _setup_logging(config: ObscuraConfig) -> None:
    """Configure structlog with JSON or console renderer."""
    try:
        from obscura.telemetry.logging import configure_logging

        configure_logging(config)
    except ImportError:
        pass


def _setup_tracing(config: ObscuraConfig) -> None:
    """Set up TracerProvider with OTLP gRPC exporter."""
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.resources import Resource

        resource = Resource.create(
            {
                "service.name": config.otel_service_name,
            }
        )

        if config.otel_enabled:
            from opentelemetry.sdk.trace.export import BatchSpanProcessor
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                OTLPSpanExporter,
            )

            provider = TracerProvider(resource=resource)
            exporter = OTLPSpanExporter(endpoint=config.otel_endpoint, insecure=True)
            provider.add_span_processor(BatchSpanProcessor(exporter))
        else:
            provider = TracerProvider(resource=resource)

        trace.set_tracer_provider(provider)

    except ImportError:
        pass


def _setup_metrics(config: ObscuraConfig) -> None:
    """Set up MeterProvider with OTLP exporter."""
    try:
        from opentelemetry import metrics
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.resources import Resource

        resource = Resource.create(
            {
                "service.name": config.otel_service_name,
            }
        )

        if config.otel_enabled:
            from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
            from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
                OTLPMetricExporter,
            )

            exporter = OTLPMetricExporter(endpoint=config.otel_endpoint, insecure=True)
            reader = PeriodicExportingMetricReader(exporter)
            provider = MeterProvider(resource=resource, metric_readers=[reader])
        else:
            provider = MeterProvider(resource=resource)

        metrics.set_meter_provider(provider)

    except ImportError:
        pass


def _setup_fastapi_instrumentation(config: ObscuraConfig) -> None:
    """Register FastAPI auto-instrumentation if available."""
    if not config.otel_enabled:
        return
    try:
        from importlib import import_module

        instrumentor_mod: Any = import_module("opentelemetry.instrumentation.fastapi")
        instrumentor_mod.FastAPIInstrumentor().instrument()
    except (ImportError, AttributeError):
        pass
