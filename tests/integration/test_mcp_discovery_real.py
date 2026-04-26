"""End-to-end smoke test for external MCP tool discovery.

Boots a real MCP server subprocess (prognostic-mcp, since it ships in
obscura's venv on dev machines) and verifies that:

  1. ``discover_mcp_tools`` lists its tools and produces ``ToolSpec``
     entries named ``mcp__<server>__<tool>``.
  2. ``register_external_mcp_tools`` lands those specs in a backend's
     ``ToolRegistry`` so they're discoverable by ``tool_search`` and the
     system-prompt builder.

The test auto-skips when no probe-able MCP binary is available, so CI
without those binaries doesn't fail.

This is the integration-shaped twin of ``tests/unit/obscura/integrations/
mcp/test_discovery.py``, which mocks the MCP client. Running both gives
coverage of the wiring (unit, fast) plus the protocol round-trip
(integration, real subprocess).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from obscura.integrations.mcp.discovery import (
    discover_mcp_tools,
    register_external_mcp_tools,
)
from obscura.providers._tool_host import BackendToolHostMixin


_VENV_BIN = Path(__file__).resolve().parents[2] / ".venv" / "bin" / "prognostic-mcp"


def _resolve_prognostic_binary() -> str | None:
    """Return a path to a runnable prognostic-mcp, or None to skip the test."""
    if _VENV_BIN.exists():
        return str(_VENV_BIN)
    found = shutil.which("prognostic-mcp")
    return found


def _prognostic_config(binary: str) -> dict[str, object]:
    return {
        "name": "prognostic",
        "transport": "stdio",
        "command": binary,
        "args": ["--transport", "stdio"],
    }


class _StubBackend(BackendToolHostMixin):
    def __init__(self) -> None:
        self._init_tool_host()


@pytest.mark.asyncio
async def test_discover_real_prognostic_server() -> None:
    """Probe the real prognostic MCP server and confirm shadow specs come back."""
    binary = _resolve_prognostic_binary()
    if binary is None:
        pytest.skip("prognostic-mcp not installed in venv or PATH")

    specs = await discover_mcp_tools([_prognostic_config(binary)], timeout=8.0)

    # The server may take a few seconds to start; if discovery returned empty,
    # something's wrong with the wiring (or the binary).
    assert specs, "discovery returned no tools — server probe likely timed out"

    names = {s.name for s in specs}
    # Tools known to be exposed by prognostic v3.x (verified manually). We don't
    # assert the full set — the server may add or rename tools — but a couple of
    # core ones must be present.
    assert "mcp__prognostic__discover_markets" in names
    assert any(name.startswith("mcp__prognostic__") for name in names)

    # Every spec must follow the namespace convention.
    for spec in specs:
        assert spec.name.startswith("mcp__prognostic__"), (
            f"unexpected spec name {spec.name!r}"
        )
        assert isinstance(spec.parameters, dict)


@pytest.mark.asyncio
async def test_register_external_lands_specs_in_backend_registry() -> None:
    """register_external_mcp_tools puts shadow specs into the backend registry."""
    binary = _resolve_prognostic_binary()
    if binary is None:
        pytest.skip("prognostic-mcp not installed in venv or PATH")

    backend = _StubBackend()
    count = await register_external_mcp_tools(
        backend, [_prognostic_config(binary)], timeout=8.0
    )

    assert count > 0, "register_external_mcp_tools registered nothing"

    # The mixin's get_tool_registry() backed registry must resolve the namespace
    # name as well as accept a query through tool_search.
    registry = backend.get_tool_registry()
    assert registry.get("mcp__prognostic__discover_markets") is not None
    # Namespace prefix lookup via the registry's stored specs.
    all_names = [s.name for s in registry.all()]
    assert any(n.startswith("mcp__prognostic__") for n in all_names)
