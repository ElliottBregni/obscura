"""Tests for MCP tool discovery — probing external MCP servers for shadow ToolSpecs."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from obscura.integrations.mcp import discovery
from obscura.integrations.mcp.types import (
    MCPConnectionConfig,
    MCPTool,
    MCPTransportType,
)


class TestBuildConfig:
    def test_stdio_with_command(self) -> None:
        cfg = discovery._build_config(
            {
                "name": "prognostic",
                "transport": "stdio",
                "command": "/usr/bin/foo",
                "args": ["--mode", "x"],
                "env": {"FOO": "1"},
            }
        )
        assert isinstance(cfg, MCPConnectionConfig)
        assert cfg.transport == MCPTransportType.STDIO
        assert cfg.command == "/usr/bin/foo"
        assert cfg.args == ["--mode", "x"]
        assert cfg.env == {"FOO": "1"}
        assert cfg.name == "prognostic"

    def test_stdio_missing_command_returns_none(self) -> None:
        assert discovery._build_config({"name": "x", "transport": "stdio"}) is None

    def test_sse_with_url(self) -> None:
        cfg = discovery._build_config(
            {
                "name": "remote",
                "transport": "sse",
                "url": "https://example.com/sse",
                "headers": {"Authorization": "Bearer x"},
            }
        )
        assert isinstance(cfg, MCPConnectionConfig)
        assert cfg.transport == MCPTransportType.SSE
        assert cfg.url == "https://example.com/sse"
        assert cfg.headers == {"Authorization": "Bearer x"}

    def test_unknown_transport_returns_none(self) -> None:
        assert (
            discovery._build_config(
                {"name": "x", "transport": "carrier-pigeon", "command": "/bin/foo"}
            )
            is None
        )


class TestProbeOneServer:
    @pytest.mark.asyncio
    async def test_returns_shadow_specs_with_namespaced_names(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Probing a server returns ToolSpecs prefixed mcp__<server>__<tool>."""
        fake_tools = [
            MCPTool(name="discover_markets", description="find markets"),
            MCPTool(name="inspect_market", description="inspect"),
        ]

        # Build a fake MCPClient that's an async context manager.
        fake_client = MagicMock()
        fake_client.__aenter__ = AsyncMock(return_value=fake_client)
        fake_client.__aexit__ = AsyncMock(return_value=None)
        fake_client.list_tools = AsyncMock(return_value=fake_tools)

        def _ctor(_config: Any) -> Any:
            return fake_client

        monkeypatch.setattr(discovery, "MCPClient", _ctor)

        specs = await discovery._probe_one_server(
            {
                "name": "prognostic",
                "transport": "stdio",
                "command": "/bin/prognostic-mcp",
            },
            timeout=1.0,
        )

        names = [s.name for s in specs]
        assert names == [
            "mcp__prognostic__discover_markets",
            "mcp__prognostic__inspect_market",
        ]

    @pytest.mark.asyncio
    async def test_failed_probe_returns_empty_list(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An exception during probing yields an empty list, not a raise."""

        fake_client = MagicMock()
        fake_client.__aenter__ = AsyncMock(side_effect=RuntimeError("connect failed"))
        fake_client.__aexit__ = AsyncMock(return_value=None)

        monkeypatch.setattr(discovery, "MCPClient", lambda _c: fake_client)

        specs = await discovery._probe_one_server(
            {
                "name": "broken",
                "transport": "stdio",
                "command": "/bin/nope",
            },
            timeout=1.0,
        )
        assert specs == []

    @pytest.mark.asyncio
    async def test_malformed_config_returns_empty(self) -> None:
        # Missing command for stdio
        specs = await discovery._probe_one_server(
            {"name": "x", "transport": "stdio"}, timeout=1.0
        )
        assert specs == []


class TestShadowHandler:
    @pytest.mark.asyncio
    async def test_shadow_handler_returns_routing_error(self) -> None:
        handler = discovery._shadow_handler_factory("mcp__svc__do_thing")
        result = await handler()
        assert "shadow_tool_invoked" in result
        assert "mcp__svc__do_thing" in result


class TestDiscoverMcpTools:
    @pytest.mark.asyncio
    async def test_empty_input_returns_empty(self) -> None:
        assert await discovery.discover_mcp_tools([]) == []

    @pytest.mark.asyncio
    async def test_aggregates_specs_across_servers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _fake_probe(server: dict[str, Any], *, timeout: float) -> list[Any]:
            from obscura.core.types import ToolSpec

            n = server["name"]
            return [
                ToolSpec(
                    name=f"mcp__{n}__t",
                    description="x",
                    parameters={"type": "object", "properties": {}},
                    handler=lambda: None,
                )
            ]

        monkeypatch.setattr(discovery, "_probe_one_server", _fake_probe)

        specs = await discovery.discover_mcp_tools(
            [{"name": "a"}, {"name": "b"}, {"name": "c"}]
        )
        assert sorted(s.name for s in specs) == [
            "mcp__a__t",
            "mcp__b__t",
            "mcp__c__t",
        ]

    @pytest.mark.asyncio
    async def test_one_server_failure_does_not_block_others(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _fake_probe(server: dict[str, Any], *, timeout: float) -> list[Any]:
            from obscura.core.types import ToolSpec

            if server["name"] == "broken":
                raise RuntimeError("kaboom")
            return [
                ToolSpec(
                    name=f"mcp__{server['name']}__ok",
                    description="x",
                    parameters={"type": "object", "properties": {}},
                    handler=lambda: None,
                )
            ]

        monkeypatch.setattr(discovery, "_probe_one_server", _fake_probe)

        specs = await discovery.discover_mcp_tools(
            [{"name": "good1"}, {"name": "broken"}, {"name": "good2"}]
        )
        # Failed server is skipped; healthy ones still contribute.
        assert sorted(s.name for s in specs) == [
            "mcp__good1__ok",
            "mcp__good2__ok",
        ]
