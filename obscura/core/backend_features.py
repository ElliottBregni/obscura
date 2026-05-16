"""Small backend capability predicates shared across construction paths."""

from __future__ import annotations

from obscura.core.enums.agent import Backend


def backend_routes_mcp_natively(backend: Backend | str | None) -> bool:
    """Return True when the backend owns MCP routing outside Obscura's executor."""
    if isinstance(backend, Backend):
        return backend == Backend.CODEX
    value = str(backend or "").lower()
    return value == Backend.CODEX.value
