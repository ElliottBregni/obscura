# pyright: reportMissingImports=false
"""
obscura.internal.tools — Unified tool definitions for both backends.

Provides a ``@tool`` decorator that creates a ``ToolSpec`` which can be
registered with either the Copilot or Claude backend. Includes basic
JSON Schema inference from function type hints.
"""

from __future__ import annotations

import functools
import inspect
from typing import Any, Callable

from obscura.core.types import ToolSpec


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------


class ToolRegistry:
    """Central registry for tool specs. Backends read from this at start()."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}
        self._alias_targets: dict[str, str] = {
            # shell
            "bash": "run_shell",
            "shell": "run_shell",
            "terminal": "run_shell",
            "runbash": "run_shell",
            "run_bash": "run_shell",
            # python — multiple naming conventions used by different LLMs
            "python": "run_python3",
            "run_python": "run_python3",
            "execute_python": "run_python3",
            "execute_code": "run_python3",
            "run_code": "run_python3",
            "code": "run_python3",
            # web search
            "websearch": "web_search",
            "web_search": "web_search",
            "searchweb": "web_search",
            "search": "web_search",
            # web fetch
            "webfetch": "web_fetch",
            "web_fetch": "web_fetch",
            "fetchurl": "web_fetch",
            "fetch": "web_fetch",
            "get_url": "web_fetch",
            "browse": "web_fetch",
            "open_url": "web_fetch",
            # task delegation
            "task": "task",
            "delegatetask": "task",
            # browser tools
            "browsernavigate": "browser_navigate",
            "browsersnapshot": "browser_snapshot",
            "browserclick": "browser_click",
            "browserfill": "browser_fill",
        }

    def register(self, spec: ToolSpec) -> None:
        self._tools[spec.name] = spec

    def register_alias(self, alias: str, canonical: str) -> None:
        """Map *alias* to an already-registered *canonical* tool name.

        Useful for runtime registration of backend-specific naming conventions::

            registry.register_alias("execute_python", "run_python3")
            registry.register_alias("google", "web_search")
        """
        self._alias_targets[_normalize_tool_name(alias)] = canonical

    def get(self, name: str) -> ToolSpec | None:
        direct = self._tools.get(name)
        if direct is not None:
            return direct
        canonical = self._alias_targets.get(_normalize_tool_name(name))
        if canonical is None:
            return None
        return self._tools.get(canonical)

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


def _normalize_tool_name(name: str) -> str:
    chars: list[str] = []
    for char in name.strip().lower():
        if char.isalnum() or char == "_":
            chars.append(char)
    return "".join(chars)


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

        setattr(wrapper, "spec", spec)
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
        from obscura.telemetry.traces import get_tracer

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
        from obscura.telemetry.traces import get_tracer

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
        from obscura.telemetry.metrics import get_metrics

        m = get_metrics()
        m.tool_calls_total.add(1, {"tool_name": tool_name, "status": status})
        m.tool_duration_seconds.record(duration, {"tool_name": tool_name})
    except Exception:
        pass
