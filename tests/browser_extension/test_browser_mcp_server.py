"""Unit tests for the in-process browser MCP server.

These tests avoid spinning up a real MCP client. They exercise the
server's start/stop/idempotency contract and confirm that the FastMCP
tool registry ends up holding one entry per ``browser_tools.TOOLS``.
"""
# pyright: reportPrivateUsage=false, reportUnknownMemberType=false, reportUnusedFunction=false

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

NATIVE_HOST_DIR = str(
    Path(__file__).parent.parent.parent
    / "packages"
    / "browser-extension"
    / "native-host"
)


def _import(name: str) -> Any:
    if NATIVE_HOST_DIR not in sys.path:
        sys.path.insert(0, NATIVE_HOST_DIR)
    if name in sys.modules:
        return sys.modules[name]
    return importlib.import_module(name)


@pytest.fixture(autouse=True)
async def _reset_server_state() -> None:
    """Make sure each test starts with the module's singleton at rest."""
    bms = _import("browser_mcp_server")
    await bms.stop_browser_mcp()


class TestBrowserMcpServer:
    @pytest.mark.asyncio
    async def test_start_returns_localhost_url(self) -> None:
        bms = _import("browser_mcp_server")
        url = await bms.start_browser_mcp()
        try:
            assert url.startswith("http://127.0.0.1:")
            assert url.endswith("/mcp")
            assert bms.current_url() == url
        finally:
            await bms.stop_browser_mcp()
            assert bms.current_url() is None

    @pytest.mark.asyncio
    async def test_start_is_idempotent(self) -> None:
        bms = _import("browser_mcp_server")
        url1 = await bms.start_browser_mcp()
        url2 = await bms.start_browser_mcp()
        try:
            assert url1 == url2
        finally:
            await bms.stop_browser_mcp()

    @pytest.mark.asyncio
    async def test_stop_is_idempotent(self) -> None:
        bms = _import("browser_mcp_server")
        # Stop-before-start is a no-op.
        await bms.stop_browser_mcp()
        await bms.stop_browser_mcp()
        assert bms.current_url() is None

    @pytest.mark.asyncio
    async def test_server_is_reachable(self) -> None:
        bms = _import("browser_mcp_server")
        url = await bms.start_browser_mcp()
        try:
            async with httpx.AsyncClient() as client:
                # MCP's streamable-HTTP endpoint refuses plain GETs with
                # 406 Not Acceptable — confirming the server is bound and
                # the MCP handler is wired up.  A connection-refused or
                # 5xx would indicate the server never came up.
                resp = await client.get(url, timeout=2.0)
                assert resp.status_code == 406
        finally:
            await bms.stop_browser_mcp()

    @pytest.mark.asyncio
    async def test_exposes_every_browser_tool(self) -> None:
        """FastMCP's tool manager should carry one entry per browser tool."""
        browser_tools = _import("browser_tools")
        bms = _import("browser_mcp_server")

        # Build the app — side effect: populates FastMCP's internal tool
        # registry.  We can read it back via the returned tool manager to
        # verify every browser tool was registered under its public name.
        from mcp.server.fastmcp import FastMCP

        mcp = FastMCP("obscura-browser", stateless_http=True)
        for spec in browser_tools.TOOLS:
            handler = bms._typed_handler(spec)
            mcp.add_tool(
                fn=handler,
                name=spec.name,
                description=spec.description,
            )

        registered = await mcp.list_tools()
        names = {t.name for t in registered}
        expected = {spec.name for spec in browser_tools.TOOLS}
        assert names == expected
        assert all(n.startswith("browser_") for n in names)

    @pytest.mark.asyncio
    async def test_typed_handler_preserves_signature(self) -> None:
        """FastMCP derives schemas from signatures; wrappers must inherit them."""
        import inspect

        browser_tools = _import("browser_tools")
        bms = _import("browser_mcp_server")

        for spec in browser_tools.TOOLS:
            wrapped = bms._typed_handler(spec)
            sig = inspect.signature(wrapped)
            original_sig = inspect.signature(spec.handler)
            assert list(sig.parameters.keys()) == list(
                original_sig.parameters.keys()
            ), f"{spec.name} signature drifted from underlying handler"
            assert wrapped.__name__ == spec.name
