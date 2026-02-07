"""
sdk.telemetry.traces — Tracer setup and ``@traced`` decorator.

Provides helpers for creating spans around sync and async functions. All
OTel imports are lazy so the SDK works without OTel installed.

Usage::

    from sdk.telemetry.traces import get_tracer, traced

    tracer = get_tracer(__name__)

    @traced("my_operation")
    async def do_work():
        ...
"""

from __future__ import annotations

import functools
import inspect
from typing import Any, Callable, TypeVar

F = TypeVar("F", bound=Callable[..., Any])


def get_tracer(name: str) -> Any:
    """Return an OTel tracer, or a no-op stub if OTel is unavailable."""
    try:
        from opentelemetry import trace
        return trace.get_tracer(name)
    except ImportError:
        return _NoOpTracer()


def set_span_attribute(key: str, value: Any) -> None:
    """Set an attribute on the current active span (if any)."""
    try:
        from opentelemetry import trace
        span = trace.get_current_span()
        if span and span.is_recording():
            span.set_attribute(key, value)
    except ImportError:
        pass


def set_span_status_error(description: str = "") -> None:
    """Mark the current span as errored."""
    try:
        from opentelemetry import trace
        from opentelemetry.trace import StatusCode
        span = trace.get_current_span()
        if span and span.is_recording():
            span.set_status(StatusCode.ERROR, description)
    except ImportError:
        pass


def record_exception(exc: BaseException) -> None:
    """Record an exception on the current span."""
    try:
        from opentelemetry import trace
        span = trace.get_current_span()
        if span and span.is_recording():
            span.record_exception(exc)
    except ImportError:
        pass


def traced(
    name: str | None = None,
    *,
    attributes: dict[str, Any] | None = None,
) -> Callable[[F], F]:
    """Decorator that wraps a function with an OTel span.

    Works with both sync and async functions. If OTel is not installed
    the original function is returned unchanged.
    """
    def decorator(fn: F) -> F:
        span_name = name or f"{fn.__module__}.{fn.__qualname__}"
        extra_attrs = attributes or {}

        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                try:
                    from opentelemetry import trace
                    tracer = trace.get_tracer(fn.__module__)
                except ImportError:
                    return await fn(*args, **kwargs)

                with tracer.start_as_current_span(span_name) as span:
                    for k, v in extra_attrs.items():
                        span.set_attribute(k, v)
                    try:
                        result = await fn(*args, **kwargs)
                        return result
                    except Exception as exc:
                        span.set_status(
                            trace.StatusCode.ERROR, str(exc),
                        )
                        span.record_exception(exc)
                        raise
            return async_wrapper  # type: ignore[return-value]
        else:
            @functools.wraps(fn)
            def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
                try:
                    from opentelemetry import trace
                    tracer = trace.get_tracer(fn.__module__)
                except ImportError:
                    return fn(*args, **kwargs)

                with tracer.start_as_current_span(span_name) as span:
                    for k, v in extra_attrs.items():
                        span.set_attribute(k, v)
                    try:
                        result = fn(*args, **kwargs)
                        return result
                    except Exception as exc:
                        span.set_status(
                            trace.StatusCode.ERROR, str(exc),
                        )
                        span.record_exception(exc)
                        raise
            return sync_wrapper  # type: ignore[return-value]

    return decorator


# ---------------------------------------------------------------------------
# No-op fallbacks
# ---------------------------------------------------------------------------

class _NoOpSpan:
    """Minimal no-op span for when OTel is unavailable."""

    def set_attribute(self, key: str, value: Any) -> None:
        pass

    def set_status(self, *args: Any, **kwargs: Any) -> None:
        pass

    def record_exception(self, exc: BaseException) -> None:
        pass

    def is_recording(self) -> bool:
        return False

    def __enter__(self) -> _NoOpSpan:
        return self

    def __exit__(self, *exc: Any) -> None:
        pass


class _NoOpTracer:
    """Minimal no-op tracer for when OTel is unavailable."""

    def start_as_current_span(self, name: str, **kwargs: Any) -> _NoOpSpan:
        return _NoOpSpan()

    def start_span(self, name: str, **kwargs: Any) -> _NoOpSpan:
        return _NoOpSpan()
