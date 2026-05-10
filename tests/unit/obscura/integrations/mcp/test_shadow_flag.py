"""Tests for is_shadow flag on MCP shadow specs (Change 2 - discovery side)."""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest


def _mock_tool(name: str, description: str = "desc") -> Any:
    t = MagicMock()
    t.name = name
    t.description = description
    t.inputSchema = {"type": "object", "properties": {}}
    return t


@pytest.mark.asyncio
async def test_discover_mcp_tools_sets_is_shadow_true() -> None:
    """Specs returned by discover_mcp_tools must have is_shadow=True."""
    from obscura.integrations.mcp.discovery import discover_mcp_tools

    fake_tools = [_mock_tool("list_files"), _mock_tool("read_file")]

    class _FakeClient:
        async def __aenter__(self) -> "_FakeClient":
            return self

        async def __aexit__(self, *_: Any) -> None:
            return None

        async def list_tools(self) -> list[Any]:
            return fake_tools

    with patch("obscura.integrations.mcp.discovery.MCPClient", return_value=_FakeClient()):
        specs = await discover_mcp_tools(
            [{"name": "test_server", "transport": "stdio", "command": "echo"}]
        )

    assert len(specs) == 2
    for spec in specs:
        assert spec.is_shadow is True, (
            f"Expected is_shadow=True on spec {spec.name!r}, got {spec.is_shadow}"
        )


@pytest.mark.asyncio
async def test_register_external_mcp_tools_registers_shadow_specs() -> None:
    """register_external_mcp_tools must register shadow specs into the backend."""
    from obscura.integrations.mcp.discovery import register_external_mcp_tools

    registered: list[Any] = []

    backend = MagicMock()

    def _register_tool(spec: Any) -> None:
        registered.append(spec)

    backend.register_tool = _register_tool

    fake_tools = [_mock_tool("alpha"), _mock_tool("beta")]

    class _FakeClient:
        async def __aenter__(self) -> "_FakeClient":
            return self

        async def __aexit__(self, *_: Any) -> None:
            return None

        async def list_tools(self) -> list[Any]:
            return fake_tools

    with patch("obscura.integrations.mcp.discovery.MCPClient", return_value=_FakeClient()):
        await register_external_mcp_tools(
            backend,
            [{"name": "myserver", "transport": "stdio", "command": "echo"}],
        )

    assert len(registered) == 2
    for spec in registered:
        assert spec.is_shadow is True
