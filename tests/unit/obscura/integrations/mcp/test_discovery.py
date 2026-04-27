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

    def test_per_server_timeout_override_honoured(self) -> None:
        """`timeout` field on the server config flows into MCPConnectionConfig."""
        cfg = discovery._build_config(
            {
                "name": "slow",
                "transport": "stdio",
                "command": "/bin/slow",
                "timeout": 15.0,
            }
        )
        assert cfg is not None
        assert cfg.timeout == 15.0

    def test_timeout_seconds_alias_accepted(self) -> None:
        cfg = discovery._build_config(
            {
                "name": "slow",
                "transport": "stdio",
                "command": "/bin/slow",
                "timeout_seconds": 12.5,
            }
        )
        assert cfg is not None
        assert cfg.timeout == 12.5

    def test_invalid_timeout_falls_back_to_default(self) -> None:
        cfg = discovery._build_config(
            {
                "name": "x",
                "transport": "stdio",
                "command": "/bin/x",
                "timeout": "not-a-number",
            }
        )
        assert cfg is not None
        assert cfg.timeout == discovery._DEFAULT_PROBE_TIMEOUT

    def test_excessive_timeout_clamped(self) -> None:
        """A 600s probe timeout would stall startup — clamp to 60s."""
        cfg = discovery._build_config(
            {
                "name": "x",
                "transport": "stdio",
                "command": "/bin/x",
                "timeout": 600.0,
            }
        )
        assert cfg is not None
        assert cfg.timeout == 60.0


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

        specs, status = await discovery._probe_one_server(
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
        assert status.ok is True
        assert status.server_name == "prognostic"
        assert status.tool_count == 2
        assert status.error is None

    @pytest.mark.asyncio
    async def test_failed_probe_yields_failure_status(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An exception during probing yields empty specs + ok=False status."""

        fake_client = MagicMock()
        fake_client.__aenter__ = AsyncMock(side_effect=RuntimeError("connect failed"))
        fake_client.__aexit__ = AsyncMock(return_value=None)

        monkeypatch.setattr(discovery, "MCPClient", lambda _c: fake_client)

        specs, status = await discovery._probe_one_server(
            {
                "name": "broken",
                "transport": "stdio",
                "command": "/bin/nope",
            },
            timeout=1.0,
        )
        assert specs == []
        assert status.ok is False
        assert status.server_name == "broken"
        assert status.tool_count == 0
        assert status.error is not None
        assert "RuntimeError" in status.error

    @pytest.mark.asyncio
    async def test_malformed_config_yields_failure_status(self) -> None:
        # Missing command for stdio
        specs, status = await discovery._probe_one_server(
            {"name": "x", "transport": "stdio"}, timeout=1.0
        )
        assert specs == []
        assert status.ok is False
        assert status.error is not None
        assert "malformed config" in status.error


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
        async def _fake_probe(
            server: dict[str, Any], *, timeout: float
        ) -> tuple[list[Any], discovery.DiscoveryStatus]:
            from obscura.core.types import ToolSpec

            n = server["name"]
            specs = [
                ToolSpec(
                    name=f"mcp__{n}__t",
                    description="x",
                    parameters={"type": "object", "properties": {}},
                    handler=lambda: None,
                )
            ]
            return specs, discovery.DiscoveryStatus(
                server_name=n, transport="stdio", ok=True, tool_count=1
            )

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
        async def _fake_probe(
            server: dict[str, Any], *, timeout: float
        ) -> tuple[list[Any], discovery.DiscoveryStatus]:
            from obscura.core.types import ToolSpec

            n = server["name"]
            if n == "broken":
                return [], discovery.DiscoveryStatus(
                    server_name=n,
                    transport="stdio",
                    ok=False,
                    tool_count=0,
                    error="RuntimeError: kaboom",
                )
            specs = [
                ToolSpec(
                    name=f"mcp__{n}__ok",
                    description="x",
                    parameters={"type": "object", "properties": {}},
                    handler=lambda: None,
                )
            ]
            return specs, discovery.DiscoveryStatus(
                server_name=n, transport="stdio", ok=True, tool_count=1
            )

        monkeypatch.setattr(discovery, "_probe_one_server", _fake_probe)

        specs = await discovery.discover_mcp_tools(
            [{"name": "good1"}, {"name": "broken"}, {"name": "good2"}]
        )
        # Failed server is skipped; healthy ones still contribute.
        assert sorted(s.name for s in specs) == [
            "mcp__good1__ok",
            "mcp__good2__ok",
        ]


class TestDiscoveryReport:
    @pytest.mark.asyncio
    async def test_report_captures_per_server_outcomes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _fake_probe(
            server: dict[str, Any], *, timeout: float
        ) -> tuple[list[Any], discovery.DiscoveryStatus]:
            from obscura.core.types import ToolSpec

            n = server["name"]
            if n == "fail":
                return [], discovery.DiscoveryStatus(
                    server_name=n,
                    transport="stdio",
                    ok=False,
                    tool_count=0,
                    error="TimeoutError: ",
                    duration_ms=4000,
                )
            return [
                ToolSpec(
                    name=f"mcp__{n}__t",
                    description="x",
                    parameters={"type": "object", "properties": {}},
                    handler=lambda: None,
                )
            ], discovery.DiscoveryStatus(
                server_name=n,
                transport="stdio",
                ok=True,
                tool_count=1,
                duration_ms=120,
            )

        monkeypatch.setattr(discovery, "_probe_one_server", _fake_probe)

        report = await discovery.discover_mcp_tools_with_report(
            [{"name": "ok1"}, {"name": "fail"}, {"name": "ok2"}]
        )

        assert {s.name for s in report.specs} == {"mcp__ok1__t", "mcp__ok2__t"}
        assert report.total_tools == 2
        assert {s.server_name for s in report.ok_servers} == {"ok1", "ok2"}
        assert {s.server_name for s in report.failed_servers} == {"fail"}

        report_dict = report.to_dict()
        assert report_dict["ok"] is False
        assert report_dict["total_tools"] == 2
        assert len(report_dict["servers"]) == 3
        # Failure entry includes the error message.
        fail_entry = next(
            s for s in report_dict["servers"] if s["name"] == "fail"
        )
        assert fail_entry["ok"] is False
        assert "TimeoutError" in fail_entry["error"]

    @pytest.mark.asyncio
    async def test_register_stashes_report_on_backend(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """register_external_mcp_tools writes the report onto the backend."""
        from obscura.providers._tool_host import BackendToolHostMixin

        async def _fake_probe(
            server: dict[str, Any], *, timeout: float
        ) -> tuple[list[Any], discovery.DiscoveryStatus]:
            from obscura.core.types import ToolSpec

            return [
                ToolSpec(
                    name=f"mcp__{server['name']}__t",
                    description="x",
                    parameters={"type": "object", "properties": {}},
                    handler=lambda: None,
                )
            ], discovery.DiscoveryStatus(
                server_name=server["name"],
                transport="stdio",
                ok=True,
                tool_count=1,
            )

        monkeypatch.setattr(discovery, "_probe_one_server", _fake_probe)

        class _Stub(BackendToolHostMixin):
            def __init__(self) -> None:
                self._init_tool_host()

        backend = _Stub()
        report = await discovery.register_external_mcp_tools(
            backend, [{"name": "x"}]
        )
        assert report.total_tools == 1
        assert backend.last_mcp_discovery_report is report
