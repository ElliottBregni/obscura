"""Tests for sdk.telemetry — init_telemetry and helpers."""
import pytest
from unittest.mock import patch, MagicMock
from sdk.config import ObscuraConfig


class TestInitTelemetry:
    def setup_method(self):
        """Reset telemetry state before each test."""
        from sdk.telemetry import _reset
        _reset()

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
        from sdk.telemetry import init_telemetry, is_initialized, _reset
        config = ObscuraConfig(otel_enabled=False)
        init_telemetry(config)
        assert is_initialized() is True
        _reset()
        assert is_initialized() is False


class TestSetupLogging:
    def test_setup_logging_import_error(self):
        from sdk.telemetry import _setup_logging
        config = ObscuraConfig(otel_enabled=False)
        with patch("sdk.telemetry.logging.configure_logging", side_effect=ImportError):
            # Should not raise
            _setup_logging(config)

    def test_setup_logging_success(self):
        from sdk.telemetry import _setup_logging
        config = ObscuraConfig(otel_enabled=False)
        with patch("sdk.telemetry.logging.configure_logging") as mock_configure:
            _setup_logging(config)
            mock_configure.assert_called_once_with(config)


class TestSetupTracing:
    def test_setup_tracing_disabled(self):
        from sdk.telemetry import _setup_tracing
        config = ObscuraConfig(otel_enabled=False)
        try:
            _setup_tracing(config)
        except ImportError:
            pass  # OTel may not be installed

    def test_setup_tracing_import_error(self):
        from sdk.telemetry import _setup_tracing
        config = ObscuraConfig(otel_enabled=False)
        with patch.dict("sys.modules", {"opentelemetry": None}):
            # Should not raise even if imports fail
            _setup_tracing(config)


class TestSetupMetrics:
    def test_setup_metrics_disabled(self):
        from sdk.telemetry import _setup_metrics
        config = ObscuraConfig(otel_enabled=False)
        try:
            _setup_metrics(config)
        except ImportError:
            pass  # OTel may not be installed


class TestSetupFastAPIInstrumentation:
    def test_fastapi_instrumentation_disabled(self):
        from sdk.telemetry import _setup_fastapi_instrumentation
        config = ObscuraConfig(otel_enabled=False)
        # Should return early without error
        _setup_fastapi_instrumentation(config)

    def test_fastapi_instrumentation_import_error(self):
        from sdk.telemetry import _setup_fastapi_instrumentation
        config = ObscuraConfig(otel_enabled=True)
        # Should not raise even without FastAPI instrumentation
        _setup_fastapi_instrumentation(config)
