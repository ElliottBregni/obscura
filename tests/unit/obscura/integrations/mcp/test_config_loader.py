"""Tests for sdk.mcp.config_loader."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from obscura.integrations.mcp.config_loader import (
    DiscoveredMCPServer,
    build_runtime_server_configs,
    discover_mcp_servers,
)
from obscura.integrations.mcp.types import MCPTransportType


class TestDiscoverMCPServers:
    def test_returns_empty_when_config_missing(self, tmp_path: Path) -> None:
        discovered = discover_mcp_servers(tmp_path / "missing.json")
        assert discovered == []

    def test_resolves_env_placeholders(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        config_path = tmp_path / "mcp-config.json"
        config_path.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "playwright": {
                            "command": "npx",
                            "args": ["-y", "@playwright/mcp@latest"],
                            "env": {"PLAYWRIGHT_TOKEN": "${PLAYWRIGHT_TOKEN}"},
                            "tools": ["browser_navigate"],
                        },
                    },
                },
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("PLAYWRIGHT_TOKEN", "token-value")

        discovered = discover_mcp_servers(config_path)
        assert len(discovered) == 1
        server = discovered[0]
        assert server.name == "playwright"
        assert server.transport is MCPTransportType.STDIO
        assert Path(server.command).name == "npx"
        assert server.args == ("-y", "@playwright/mcp@latest")
        assert server.env["PLAYWRIGHT_TOKEN"] == "token-value"
        assert server.tools == ("browser_navigate",)
        assert server.missing_env == ()

    def test_resolves_header_placeholders(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        config_path = tmp_path / "mcp-config.json"
        config_path.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "supabase": {
                            "type": "http",
                            "url": "https://mcp.example.com/mcp?project_ref=abc",
                            "headers": {
                                "Authorization": "Bearer ${SUPABASE_ACCESS_TOKEN}",
                            },
                        },
                    },
                },
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("SUPABASE_ACCESS_TOKEN", "sb-pat-123")

        discovered = discover_mcp_servers(config_path)
        assert len(discovered) == 1
        server = discovered[0]
        assert server.transport is MCPTransportType.SSE
        assert server.headers == {"Authorization": "Bearer sb-pat-123"}
        assert server.missing_env == ()

    def test_missing_header_env_records_in_missing_env(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        config_path = tmp_path / "mcp-config.json"
        config_path.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "supabase": {
                            "type": "http",
                            "url": "https://mcp.example.com/mcp",
                            "headers": {
                                "Authorization": "Bearer ${SUPABASE_ACCESS_TOKEN}",
                            },
                        },
                    },
                },
            ),
            encoding="utf-8",
        )
        monkeypatch.delenv("SUPABASE_ACCESS_TOKEN", raising=False)

        discovered = discover_mcp_servers(config_path)
        server = discovered[0]
        # Inline substitution leaves the surrounding literal in place when
        # the env var is unset; the empty replacement is what flags the gap.
        assert server.headers == {"Authorization": "Bearer "}
        assert "SUPABASE_ACCESS_TOKEN" in server.missing_env

    def test_raises_for_unknown_transport(self, tmp_path: Path) -> None:
        config_path = tmp_path / "mcp-config.json"
        config_path.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "bad": {
                            "transport": "websocket",
                            "url": "ws://localhost:8787",
                        },
                    },
                },
            ),
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="Unsupported MCP transport"):
            discover_mcp_servers(config_path)

    def test_loads_and_merges_directory_configs(self, tmp_path: Path) -> None:
        config_dir = tmp_path / ".obscura" / "mcp"
        config_dir.mkdir(parents=True)
        (config_dir / "a.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "github": {"command": "npx", "args": ["-y", "github-mcp"]},
                    },
                },
            ),
            encoding="utf-8",
        )
        (config_dir / "b.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "supabase": {"command": "npx", "args": ["-y", "supabase-mcp"]},
                    },
                },
            ),
            encoding="utf-8",
        )

        discovered = discover_mcp_servers(config_dir)
        names = {server.name for server in discovered}
        assert {"github", "supabase"} == names


class TestBuildRuntimeServerConfigs:
    def test_builds_stdio_and_sse_configs(self) -> None:
        discovered = [
            DiscoveredMCPServer(
                name="playwright",
                transport=MCPTransportType.STDIO,
                command="npx",
                args=("-y", "@playwright/mcp@latest"),
                url="",
                env={"PW_TOKEN": "x"},
                tools=("browser_navigate",),
                missing_env=(),
            ),
            DiscoveredMCPServer(
                name="remote",
                transport=MCPTransportType.SSE,
                command="",
                args=(),
                url="https://example.com/sse",
                env={},
                tools=(),
                missing_env=(),
            ),
        ]

        configs = build_runtime_server_configs(discovered)
        assert len(configs) == 2
        assert configs[0]["transport"] == "stdio"
        assert Path(str(configs[0]["command"])).name == "npx"
        assert configs[0]["tools"] == ["browser_navigate"]
        assert configs[1]["transport"] == "sse"
        assert configs[1]["url"] == "https://example.com/sse"

    def test_forwards_headers_for_http_servers(self) -> None:
        discovered = [
            DiscoveredMCPServer(
                name="supabase",
                transport=MCPTransportType.SSE,
                command="",
                args=(),
                url="https://mcp.example.com/mcp",
                env={},
                tools=(),
                missing_env=(),
                headers={"Authorization": "Bearer pat-xyz"},
            ),
        ]

        configs = build_runtime_server_configs(discovered)
        assert configs[0]["headers"] == {"Authorization": "Bearer pat-xyz"}

    def test_omits_headers_key_when_empty(self) -> None:
        discovered = [
            DiscoveredMCPServer(
                name="remote",
                transport=MCPTransportType.SSE,
                command="",
                args=(),
                url="https://example.com/sse",
                env={},
                tools=(),
                missing_env=(),
            ),
        ]

        configs = build_runtime_server_configs(discovered)
        assert "headers" not in configs[0]

    def test_selects_named_servers(self) -> None:
        discovered = [
            DiscoveredMCPServer(
                name="one",
                transport=MCPTransportType.STDIO,
                command="cmd-one",
                args=(),
                url="",
                env={},
                tools=(),
                missing_env=(),
            ),
            DiscoveredMCPServer(
                name="two",
                transport=MCPTransportType.STDIO,
                command="cmd-two",
                args=(),
                url="",
                env={},
                tools=(),
                missing_env=(),
            ),
        ]

        configs = build_runtime_server_configs(discovered, selected_names=["two"])
        assert len(configs) == 1
        assert configs[0]["command"] == "cmd-two"

    def test_orders_by_primary_server_when_unfiltered(self) -> None:
        discovered = [
            DiscoveredMCPServer(
                name="jira",
                transport=MCPTransportType.STDIO,
                command="jira",
                args=(),
                url="",
                env={},
                tools=(),
                missing_env=(),
            ),
            DiscoveredMCPServer(
                name="github",
                transport=MCPTransportType.STDIO,
                command="github",
                args=(),
                url="",
                env={},
                tools=(),
                missing_env=(),
            ),
        ]
        configs = build_runtime_server_configs(
            discovered,
            primary_server_name="github",
        )
        assert configs[0]["command"] == "github"
        assert configs[1]["command"] == "jira"

    def test_preserves_selected_order(self) -> None:
        discovered = [
            DiscoveredMCPServer(
                name="one",
                transport=MCPTransportType.STDIO,
                command="one",
                args=(),
                url="",
                env={},
                tools=(),
                missing_env=(),
            ),
            DiscoveredMCPServer(
                name="two",
                transport=MCPTransportType.STDIO,
                command="two",
                args=(),
                url="",
                env={},
                tools=(),
                missing_env=(),
            ),
        ]
        configs = build_runtime_server_configs(
            discovered,
            selected_names=["two", "one"],
        )
        assert [config["command"] for config in configs] == ["two", "one"]

    def test_raises_for_unknown_server_name(self) -> None:
        discovered = [
            DiscoveredMCPServer(
                name="known",
                transport=MCPTransportType.STDIO,
                command="npx",
                args=(),
                url="",
                env={},
                tools=(),
                missing_env=(),
            ),
        ]
        with pytest.raises(ValueError, match="Unknown MCP server"):
            build_runtime_server_configs(discovered, selected_names=["missing"])
