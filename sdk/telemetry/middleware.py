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

import uuid
from typing import Any, Callable

try:
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.requests import Request
    from starlette.responses import Response

    _HAS_STARLETTE = True
except ImportError:
    _HAS_STARLETTE = False


if _HAS_STARLETTE:

    class ObscuraTelemetryMiddleware(BaseHTTPMiddleware):
        """ASGI middleware that enriches OTel spans with user identity and request IDs."""

        async def dispatch(
            self, request: Request, call_next: Callable[..., Any],
        ) -> Response:
            # Generate request ID
            request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))

            # Enrich the current OTel span
            _enrich_request_span(request, request_id)

            # Process request
            response: Response = await call_next(request)

            # Add response headers
            response.headers["X-Request-ID"] = request_id

            # Propagate traceparent if available
            traceparent = _get_traceparent()
            if traceparent:
                response.headers["traceparent"] = traceparent

            return response

else:

    class ObscuraTelemetryMiddleware:  # pyright: ignore[reportRedeclaration]
        """Stub middleware when Starlette is not installed."""

        def __init__(self, app: Any) -> None:
            self.app = app

        async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
            await self.app(scope, receive, send)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _enrich_request_span(request: Any, request_id: str) -> None:
    """Add user identity and request metadata to the active OTel span."""
    try:
        from opentelemetry import trace

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


def _get_traceparent() -> str | None:
    """Build a W3C traceparent header from the current OTel context."""
    try:
        from opentelemetry import trace

        span = trace.get_current_span()
        ctx = span.get_span_context()
        if ctx and ctx.trace_id:
            trace_id = format(ctx.trace_id, "032x")
            span_id = format(ctx.span_id, "016x")
            flags = format(ctx.trace_flags, "02x")
            return f"00-{trace_id}-{span_id}-{flags}"
    except (ImportError, AttributeError):
        pass
    return None
