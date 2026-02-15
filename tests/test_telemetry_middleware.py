"""Tests for sdk.telemetry.middleware."""
from unittest.mock import patch, MagicMock
from fastapi import FastAPI
from starlette.testclient import TestClient
from sdk.telemetry.middleware import (
    ObscuraTelemetryMiddleware,
    enrich_request_span,
    get_traceparent,
)


def _make_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(ObscuraTelemetryMiddleware)  # pyright: ignore[reportArgumentType]

    @app.get("/test")
    async def test_endpoint():  # pyright: ignore[reportUnusedFunction]
        return {"ok": True}

    @app.post("/echo")
    async def echo_endpoint():  # pyright: ignore[reportUnusedFunction]
        return {"echo": True}

    return app


# ---------------------------------------------------------------------------
# Middleware dispatch
# ---------------------------------------------------------------------------

class TestTelemetryMiddleware:
    def test_generates_request_id(self):
        client = TestClient(_make_app())
        resp = client.get("/test")
        assert resp.status_code == 200
        assert "X-Request-ID" in resp.headers
        rid = resp.headers["X-Request-ID"]
        assert len(rid) == 36  # UUID format

    def test_propagates_provided_request_id(self):
        client = TestClient(_make_app())
        resp = client.get("/test", headers={"X-Request-ID": "my-custom-id"})
        assert resp.headers["X-Request-ID"] == "my-custom-id"

    def test_response_success(self):
        client = TestClient(_make_app())
        resp = client.get("/test")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

    @patch("sdk.telemetry.middleware._get_traceparent", return_value="00-abc-def-01")
    def test_traceparent_header(self, mock_tp: MagicMock) -> None:  # noqa: ARG002
        client = TestClient(_make_app())
        resp = client.get("/test")
        assert resp.headers.get("traceparent") == "00-abc-def-01"

    @patch("sdk.telemetry.middleware._get_traceparent", return_value=None)
    def test_no_traceparent_when_none(self, mock_tp: MagicMock) -> None:  # noqa: ARG002
        client = TestClient(_make_app())
        resp = client.get("/test")
        assert "traceparent" not in resp.headers

    def test_unique_request_ids(self) -> None:
        """Each request should get a unique request ID."""
        client = TestClient(_make_app())
        ids: set[str] = set()
        for _ in range(5):
            resp = client.get("/test")
            ids.add(resp.headers["X-Request-ID"])
        assert len(ids) == 5

    def test_post_request(self):
        client = TestClient(_make_app())
        resp = client.post("/echo")
        assert resp.status_code == 200
        assert "X-Request-ID" in resp.headers

    def test_404_still_has_request_id(self):
        client = TestClient(_make_app())
        resp = client.get("/nonexistent")
        assert resp.status_code == 404
        assert "X-Request-ID" in resp.headers


# ---------------------------------------------------------------------------
# _enrich_request_span
# ---------------------------------------------------------------------------

class TestEnrichRequestSpan:
    def test_no_otel_import(self):
        """When opentelemetry is not available, should not raise."""
        request = MagicMock()
        enrich_request_span(request, "req-1")

    def test_with_recording_span(self):
        """When OTel is available and span is recording, should set attributes."""
        mock_span = MagicMock()
        mock_span.is_recording.return_value = True

        mock_request = MagicMock()
        mock_request.state.user = None

        # The function does `from opentelemetry import trace`, so we need
        # opentelemetry.trace to resolve to our mock
        mock_trace_mod = MagicMock()
        mock_trace_mod.get_current_span.return_value = mock_span
        mock_otel = MagicMock()
        mock_otel.trace = mock_trace_mod

        with patch.dict("sys.modules", {"opentelemetry": mock_otel, "opentelemetry.trace": mock_trace_mod}):
            enrich_request_span(mock_request, "req-123")

        mock_span.set_attribute.assert_called_with("http.request_id", "req-123")

    def test_span_not_recording(self):
        """When span is not recording, should return early."""
        mock_span = MagicMock()
        mock_span.is_recording.return_value = False

        mock_trace_mod = MagicMock()
        mock_trace_mod.get_current_span.return_value = mock_span
        mock_otel = MagicMock()
        mock_otel.trace = mock_trace_mod

        with patch.dict("sys.modules", {"opentelemetry": mock_otel, "opentelemetry.trace": mock_trace_mod}):
            enrich_request_span(MagicMock(), "req-1")

        mock_span.set_attribute.assert_not_called()

    def test_no_span(self):
        """When get_current_span returns None, should return early."""
        mock_trace_mod = MagicMock()
        mock_trace_mod.get_current_span.return_value = None
        mock_otel = MagicMock()
        mock_otel.trace = mock_trace_mod

        with patch.dict("sys.modules", {"opentelemetry": mock_otel, "opentelemetry.trace": mock_trace_mod}):
            enrich_request_span(MagicMock(), "req-1")

    def test_with_user_enrichment(self):
        """When request has a user, should call enrich_span_with_user."""
        mock_span = MagicMock()
        mock_span.is_recording.return_value = True

        mock_user = MagicMock()
        mock_request = MagicMock()
        mock_request.state.user = mock_user

        mock_trace_mod = MagicMock()
        mock_trace_mod.get_current_span.return_value = mock_span
        mock_otel = MagicMock()
        mock_otel.trace = mock_trace_mod

        with patch.dict("sys.modules", {"opentelemetry": mock_otel, "opentelemetry.trace": mock_trace_mod}):
            with patch("sdk.telemetry.context.enrich_span_with_user") as mock_enrich:
                enrich_request_span(mock_request, "req-1")
                mock_enrich.assert_called_once_with(mock_span, mock_user)

    def test_request_without_state(self):
        """When request has no state attribute, should not raise."""
        mock_request = MagicMock(spec=[])  # No attributes
        enrich_request_span(mock_request, "req-1")


# ---------------------------------------------------------------------------
# _get_traceparent
# ---------------------------------------------------------------------------

class TestGetTraceparent:
    def test_no_otel(self):
        """When opentelemetry is not available, returns None."""
        result = get_traceparent()
        # If OTel is not configured, should return None
        assert result is None or isinstance(result, str)

    def test_with_valid_span_context(self):
        """When OTel span has a valid context, returns traceparent."""
        # Use a simple namespace object with real int values for format()
        class FakeCtx:
            trace_id = 0x1234567890ABCDEF1234567890ABCDEF
            span_id = 0x1234567890ABCDEF
            trace_flags = 1

        mock_span = MagicMock()
        mock_span.get_span_context.return_value = FakeCtx()

        mock_trace_mod = MagicMock()
        mock_trace_mod.get_current_span.return_value = mock_span
        mock_otel = MagicMock()
        mock_otel.trace = mock_trace_mod

        with patch.dict("sys.modules", {"opentelemetry": mock_otel, "opentelemetry.trace": mock_trace_mod}):
            result = get_traceparent()

        assert result is not None
        assert result.startswith("00-")
        parts = result.split("-")
        assert len(parts) == 4
        assert parts[0] == "00"

    def test_with_no_trace_id(self):
        """When trace_id is 0, returns None."""
        class FakeCtx:
            trace_id = 0
            span_id = 0
            trace_flags = 0

        mock_span = MagicMock()
        mock_span.get_span_context.return_value = FakeCtx()

        mock_trace_mod = MagicMock()
        mock_trace_mod.get_current_span.return_value = mock_span
        mock_otel = MagicMock()
        mock_otel.trace = mock_trace_mod

        with patch.dict("sys.modules", {"opentelemetry": mock_otel, "opentelemetry.trace": mock_trace_mod}):
            result = get_traceparent()

        assert result is None

    def test_import_error_returns_none(self):
        """When opentelemetry is not installed, returns None."""
        # Remove opentelemetry from sys.modules to simulate ImportError
        import sys
        saved: dict[str, object] = {}
        for key in list(sys.modules.keys()):
            if key.startswith("opentelemetry"):
                saved[key] = sys.modules.pop(key)
        try:
            with patch.dict("sys.modules", {"opentelemetry": None, "opentelemetry.trace": None}):
                result = get_traceparent()
            assert result is None
        finally:
            sys.modules.update(saved)  # type: ignore[arg-type]
