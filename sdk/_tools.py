"""
sdk._tools — Unified tool definitions for both backends.

Provides a ``@tool`` decorator that creates a ``ToolSpec`` which can be
registered with either the Copilot or Claude backend. Includes basic
JSON Schema inference from function type hints.
"""

from __future__ import annotations

import functools
import inspect
from typing import Any, Callable

from sdk._types import ToolSpec


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


# ---------------------------------------------------------------------------
# Schema inference
# ---------------------------------------------------------------------------

_TYPE_MAP: dict[type, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
}


def _infer_schema_from_hints(fn: Callable) -> dict[str, Any]:
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
    pydantic_model: type | None = None,
) -> Callable:
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
    def decorator(fn: Callable) -> Callable:
        schema = parameters
        if schema is None and pydantic_model is not None:
            schema = pydantic_model.model_json_schema()
        elif schema is None:
            schema = _infer_schema_from_hints(fn)

        spec = ToolSpec(
            name=name,
            description=description,
            parameters=schema or {},
            handler=fn,
            _pydantic_model=pydantic_model,
        )

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            return fn(*args, **kwargs)

        wrapper.spec = spec  # type: ignore[attr-defined]
        return wrapper

    return decorator
