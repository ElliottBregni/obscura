"""Reusable tool helpers for tests.

Consolidates ``echo_handler``, ``failing_handler``, ``make_tool``, and
``_make_registry`` patterns scattered across test files.

Usage::

    from obscura.testing import make_tool, make_registry, echo_handler

    spec = make_tool("search", handler=echo_handler, params={"query": {"type": "string"}})
    registry = make_registry(spec)
"""

from __future__ import annotations

from typing import Any

from obscura.core.tools import ToolRegistry
from obscura.core.types import ToolSpec

__all__ = [
    "echo_handler",
    "async_echo_handler",
    "failing_handler",
    "noop_handler",
    "make_tool",
    "make_registry",
]


# ---------------------------------------------------------------------------
# Stock handlers
# ---------------------------------------------------------------------------


def echo_handler(**kwargs: Any) -> dict[str, Any]:
    """Return whatever was passed in — useful for verifying tool args."""
    return {"echo": kwargs}


async def async_echo_handler(**kwargs: Any) -> dict[str, Any]:
    """Async version of :func:`echo_handler`."""
    return {"echo": kwargs}


def failing_handler(**kwargs: Any) -> str:
    """Always raises RuntimeError — useful for error-path tests."""
    raise RuntimeError("boom")


def noop_handler(**kwargs: Any) -> str:
    """Returns an empty string — minimal handler for tools that don't matter."""
    return ""


# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------


def make_tool(
    name: str = "test_tool",
    *,
    description: str = "",
    handler: Any = None,
    params: dict[str, Any] | None = None,
) -> ToolSpec:
    """Create a :class:`ToolSpec` with sensible defaults.

    Parameters:
        name: Tool name.
        description: Tool description (defaults to ``"Test tool: {name}"``).
        handler: Callable handler. Defaults to :func:`echo_handler`.
        params: JSON-Schema properties dict.  Wrapped in ``{"type": "object", "properties": ...}``.
    """
    if handler is None:
        handler = echo_handler
    schema: dict[str, Any] = {"type": "object"}
    if params:
        schema["properties"] = params
    return ToolSpec(
        name=name,
        description=description or f"Test tool: {name}",
        parameters=schema,
        handler=handler,
    )


def make_registry(*specs: ToolSpec) -> ToolRegistry:
    """Create a :class:`ToolRegistry` populated with *specs*."""
    reg = ToolRegistry()
    for s in specs:
        reg.register(s)
    return reg
