# pyright: reportMissingImports=false
"""
sdk.telemetry.middleware — ASGI middleware for request tracing.

Enriches each request span with user identity, request ID, and
propagates W3C traceparent headers. Uses
``opentelemetry-instrumentation-fastapi`` for the underlying
request/response tracing.

Usage::

    from fastapi import FastAPI
    from sdk.telemetry.middleware import ObscuraTelemetryMiddleware

    app = FastAPI()
    app.add_middleware(ObscuraTelemetryMiddleware)
"""

from __future__ import annotations

import importlib
import uuid
from typing import Any, Callable

try:
    from starlette.middleware.base import BaseHTTPMiddleware

    _has_starlette = True
except ImportError:
    BaseHTTPMiddleware = Any
    Request = Any
    Response = Any
    _has_starlette = False


class ObscuraTelemetryMiddleware(BaseHTTPMiddleware):  # type: ignore[misc]
    """ASGI middleware that enriches OTel spans with user identity and request IDs."""

    async def dispatch(
        self,
        request: Any,
        call_next: Callable[..., Any],
    ) -> Any:
        if not _has_starlette:
            raise ImportError("Starlette is required for ObscuraTelemetryMiddleware")

        # Generate request ID
        request_id: str = request.headers.get("X-Request-ID", str(uuid.uuid4()))

        # Enrich the current OTel span
        enrich_request_span(request, request_id)

        # Process request
        response: Any = await call_next(request)

        # Add response headers
        response.headers["X-Request-ID"] = request_id

        # Propagate traceparent if available
        traceparent = _get_traceparent()
        if traceparent:
            response.headers["traceparent"] = traceparent

        return response


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def enrich_request_span(request: Any, request_id: str) -> None:
    """Add user identity and request metadata to the active OTel span."""
    try:
        trace = importlib.import_module("opentelemetry.trace")
        span = trace.get_current_span()
        if not span or not span.is_recording():
            return

        span.set_attribute("http.request_id", request_id)

        # Extract user from request.state (set by auth middleware)
        user = getattr(getattr(request, "state", None), "user", None)
        if user is not None:
            from sdk.telemetry.context import enrich_span_with_user

            enrich_span_with_user(span, user)

    except (ImportError, AttributeError):
        pass


def get_traceparent() -> str | None:
    """Build a W3C traceparent header from the current OTel context."""
    try:
        trace = importlib.import_module("opentelemetry.trace")
        span = trace.get_current_span()
        ctx = getattr(span, "get_span_context", lambda: None)()
        if ctx and getattr(ctx, "trace_id", 0):
            trace_id = format(ctx.trace_id, "032x")
            span_id = format(ctx.span_id, "016x")
            flags = format(ctx.trace_flags, "02x")
            return f"00-{trace_id}-{span_id}-{flags}"
    except (ImportError, AttributeError):
        pass
    return None


# Backwards-compat for tests expecting _get_traceparent
def _get_traceparent() -> str | None:  # pragma: no cover - thin alias
    return get_traceparent()
