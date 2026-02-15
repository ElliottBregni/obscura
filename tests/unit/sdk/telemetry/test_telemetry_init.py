"""Tests for sdk.telemetry — init_telemetry and helpers."""

from unittest.mock import patch
from sdk.config import ObscuraConfig


class TestInitTelemetry:
    def setup_method(self):
        """Reset telemetry state before each test."""
        from sdk.telemetry import reset_telemetry

        reset_telemetry()

    def test_init_telemetry_disabled(self):
        from sdk.telemetry import init_telemetry, is_initialized

        config = ObscuraConfig(otel_enabled=False)
        init_telemetry(config)
        assert is_initialized() is True

    def test_init_telemetry_idempotent(self):
        from sdk.telemetry import init_telemetry, is_initialized

        config = ObscuraConfig(otel_enabled=False)
        init_telemetry(config)
        init_telemetry(config)  # Should not raise
        assert is_initialized() is True

    def test_reset(self):
        from sdk.telemetry import init_telemetry, is_initialized, reset_telemetry

        config = ObscuraConfig(otel_enabled=False)
        init_telemetry(config)
        assert is_initialized() is True
        reset_telemetry()
        assert is_initialized() is False


class TestSetupLogging:
    def test_setup_logging_import_error(self):
        from sdk.telemetry import setup_logging

        config = ObscuraConfig(otel_enabled=False)
        with patch("sdk.telemetry.logging.configure_logging", side_effect=ImportError):
            # Should not raise
            setup_logging(config)

    def test_setup_logging_success(self):
        from sdk.telemetry import setup_logging

        config = ObscuraConfig(otel_enabled=False)
        with patch("sdk.telemetry.logging.configure_logging") as mock_configure:
            setup_logging(config)
            mock_configure.assert_called_once_with(config)


class TestSetupTracing:
    def test_setup_tracing_disabled(self):
        from sdk.telemetry import setup_tracing

        config = ObscuraConfig(otel_enabled=False)
        try:
            setup_tracing(config)
        except ImportError:
            pass  # OTel may not be installed

    def test_setup_tracing_import_error(self):
        from sdk.telemetry import setup_tracing

        config = ObscuraConfig(otel_enabled=False)
        with patch.dict("sys.modules", {"opentelemetry": None}):
            # Should not raise even if imports fail
            setup_tracing(config)


class TestSetupMetrics:
    def test_setup_metrics_disabled(self):
        from sdk.telemetry import setup_metrics

        config = ObscuraConfig(otel_enabled=False)
        try:
            setup_metrics(config)
        except ImportError:
            pass  # OTel may not be installed


class TestSetupFastAPIInstrumentation:
    def test_fastapi_instrumentation_disabled(self):
        from sdk.telemetry import setup_fastapi_instrumentation

        config = ObscuraConfig(otel_enabled=False)
        # Should return early without error
        setup_fastapi_instrumentation(config)

    def test_fastapi_instrumentation_import_error(self):
        from sdk.telemetry import setup_fastapi_instrumentation

        config = ObscuraConfig(otel_enabled=True)
        # Should not raise even without FastAPI instrumentation
        setup_fastapi_instrumentation(config)
