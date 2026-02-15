# pyright: reportMissingImports=false
"""
sdk.internal.tools — Unified tool definitions for both backends.

Provides a ``@tool`` decorator that creates a ``ToolSpec`` which can be
registered with either the Copilot or Claude backend. Includes basic
JSON Schema inference from function type hints.
"""

from __future__ import annotations

import functools
import inspect
from typing import Any, Callable

from sdk.internal.types import ToolSpec


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------


class ToolRegistry:
    """Central registry for tool specs. Backends read from this at start()."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def all(self) -> list[ToolSpec]:
        return list(self._tools.values())

    def names(self) -> list[str]:
        return list(self._tools.keys())

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def for_tier(self, tier_value: str) -> list[ToolSpec]:
        """Return tools accessible at *tier_value*.

        ``"privileged"`` gets all tools; ``"public"`` gets only those
        with ``required_tier == "public"``.
        """
        # TODO: restrict public tier once tier differentiation is enabled
        return self.all()

    def names_for_tier(self, tier_value: str) -> list[str]:
        """Return tool names accessible at *tier_value*."""
        return [t.name for t in self.for_tier(tier_value)]


# ---------------------------------------------------------------------------
# Schema inference
# ---------------------------------------------------------------------------

_TYPE_MAP: dict[type[Any], str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
}


def infer_schema_from_hints(fn: Callable[..., Any]) -> dict[str, Any]:
    """Basic JSON Schema inference from function type hints.

    Handles simple types (str, int, float, bool). For anything more complex,
    pass an explicit schema or use a Pydantic model.
    """
    hints = inspect.get_annotations(fn, eval_str=True)
    sig = inspect.signature(fn)

    properties: dict[str, Any] = {}
    required: list[str] = []

    for param_name, param in sig.parameters.items():
        if param_name in ("self", "cls", "return"):
            continue

        hint = hints.get(param_name, str)
        json_type = _TYPE_MAP.get(hint, "string")
        properties[param_name] = {"type": json_type}

        if param.default is inspect.Parameter.empty:
            required.append(param_name)

    return {
        "type": "object",
        "properties": properties,
        "required": required,
    }


# ---------------------------------------------------------------------------
# @tool decorator
# ---------------------------------------------------------------------------


def tool(
    name: str,
    description: str,
    parameters: dict[str, Any] | None = None,
    *,
    pydantic_model: type[Any] | None = None,
    required_tier: str = "public",
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator to define a tool that works with both backends.

    Usage::

        @tool("read_file", "Read a file from disk", {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        })
        def read_file(path: str) -> str:
            return Path(path).read_text()

    The decorated function gains a ``.spec`` attribute (``ToolSpec``) that
    the client uses for registration. The function itself remains callable.

    If *parameters* is omitted and *pydantic_model* is provided, the schema
    is generated from the Pydantic model. If both are omitted, a basic schema
    is inferred from type hints.
    """

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        schema = parameters
        if schema is None and pydantic_model is not None:
            schema = pydantic_model.model_json_schema()
        elif schema is None:
            schema = infer_schema_from_hints(fn)

        spec = ToolSpec(
            name=name,
            description=description,
            parameters=schema or {},
            handler=fn,
            _pydantic_model=pydantic_model,
            required_tier=required_tier,
        )

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            if inspect.iscoroutinefunction(fn):
                return _traced_tool_call_async(name, fn, *args, **kwargs)
            return _traced_tool_call(name, fn, *args, **kwargs)

        wrapper.spec = spec  # type: ignore[attr-defined]
        return wrapper

    return decorator


# ---------------------------------------------------------------------------
# Traced tool execution
# ---------------------------------------------------------------------------


def _traced_tool_call(
    tool_name: str, fn: Callable[..., Any], *args: Any, **kwargs: Any
) -> Any:
    """Execute a tool handler wrapped in an OTel span."""
    try:
        from sdk.telemetry.traces import get_tracer

        tracer = get_tracer("obscura.tools")
    except Exception:
        return fn(*args, **kwargs)

    import time

    with tracer.start_as_current_span(f"tool.{tool_name}") as span:
        span.set_attribute("tool.name", tool_name)
        start = time.monotonic()
        try:
            result = fn(*args, **kwargs)
            _record_tool_metric(tool_name, "success", time.monotonic() - start)
            return result
        except Exception as exc:
            _record_tool_metric(tool_name, "error", time.monotonic() - start)
            try:
                from opentelemetry.trace import StatusCode

                span.set_status(StatusCode.ERROR, str(exc))
                span.record_exception(exc)
            except ImportError:
                pass
            raise


async def _traced_tool_call_async(
    tool_name: str, fn: Callable[..., Any], *args: Any, **kwargs: Any
) -> Any:
    """Execute an async tool handler wrapped in an OTel span."""
    try:
        from sdk.telemetry.traces import get_tracer

        tracer = get_tracer("obscura.tools")
    except Exception:
        return await fn(*args, **kwargs)

    import time

    with tracer.start_as_current_span(f"tool.{tool_name}") as span:
        span.set_attribute("tool.name", tool_name)
        start = time.monotonic()
        try:
            result = await fn(*args, **kwargs)
            _record_tool_metric(tool_name, "success", time.monotonic() - start)
            return result
        except Exception as exc:
            _record_tool_metric(tool_name, "error", time.monotonic() - start)
            try:
                from opentelemetry.trace import StatusCode

                span.set_status(StatusCode.ERROR, str(exc))
                span.record_exception(exc)
            except ImportError:
                pass
            raise


def _record_tool_metric(tool_name: str, status: str, duration: float) -> None:
    """Record tool call metrics."""
    try:
        from sdk.telemetry.metrics import get_metrics

        m = get_metrics()
        m.tool_calls_total.add(1, {"tool_name": tool_name, "status": status})
        m.tool_duration_seconds.record(duration, {"tool_name": tool_name})
    except Exception:
        pass
