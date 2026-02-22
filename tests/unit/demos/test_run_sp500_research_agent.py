"""Tests for demos.research.run_sp500_research_agent."""
# pyright: reportPrivateUsage=false

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from demos.research.run_sp500_research_agent import (
    _filter_mcp_server_configs,
    _runtime_mcp_config_to_connection,
)
from obscura.integrations.mcp.types import MCPTransportType


def test_runtime_mcp_config_to_connection_stdio() -> None:
    cfg = _runtime_mcp_config_to_connection(
        {
            "transport": "stdio",
            "command": "npx",
            "args": ["-y", "@playwright/mcp"],
            "env": {"A": "B"},
        }
    )
    assert cfg.transport is MCPTransportType.STDIO
    assert cfg.command == "npx"
    assert cfg.args == ["-y", "@playwright/mcp"]
    assert cfg.env == {"A": "B"}


@pytest.mark.asyncio
async def test_filter_mcp_server_configs_keeps_healthy_servers() -> None:
    created: list[MagicMock] = []

    def _factory(_servers: object) -> MagicMock:
        backend = MagicMock()
        backend.start = AsyncMock(return_value=None)
        backend.stop = AsyncMock(return_value=None)
        backend.connection_errors = {}
        backend.list_servers = MagicMock(return_value=["mcp_server_0"])
        created.append(backend)
        return backend

    configs = [
        {"transport": "stdio", "command": "one"},
        {"transport": "stdio", "command": "two"},
    ]
    with patch(
        "demos.research.run_sp500_research_agent.MCPBackend",
        side_effect=_factory,
    ):
        with patch("demos.research.run_sp500_research_agent.shutil.which", return_value="/usr/bin/fake"):
            kept = await _filter_mcp_server_configs(configs, timeout_seconds=2.0)
    assert kept == configs
    assert len(created) == 2
    created[0].start.assert_awaited_once()
    created[1].start.assert_awaited_once()


@pytest.mark.asyncio
async def test_filter_mcp_server_configs_drops_timeout_servers() -> None:
    created: list[MagicMock] = []
    start_side_effects = [asyncio.TimeoutError(), None]

    def _factory(_servers: object) -> MagicMock:
        backend = MagicMock()
        effect = start_side_effects[len(created)]
        backend.start = AsyncMock(side_effect=effect)
        backend.stop = AsyncMock(return_value=None)
        backend.connection_errors = {}
        backend.list_servers = MagicMock(return_value=["mcp_server_0"])
        created.append(backend)
        return backend

    configs = [
        {"transport": "stdio", "command": "slow"},
        {"transport": "stdio", "command": "ok"},
    ]
    with patch(
        "demos.research.run_sp500_research_agent.MCPBackend",
        side_effect=_factory,
    ):
        with patch("demos.research.run_sp500_research_agent.shutil.which", return_value="/usr/bin/fake"):
            kept = await _filter_mcp_server_configs(configs, timeout_seconds=1.0)
    assert kept == [configs[1]]


@pytest.mark.asyncio
async def test_filter_mcp_server_configs_drops_connection_error_servers() -> None:
    def _factory(_servers: object) -> MagicMock:
        backend = MagicMock()
        backend.start = AsyncMock(return_value=None)
        backend.stop = AsyncMock(return_value=None)
        backend.connection_errors = {"mcp_server_0": TimeoutError()}
        backend.list_servers = MagicMock(return_value=[])
        return backend

    cfg = {"transport": "stdio", "command": "bad"}
    with patch(
        "demos.research.run_sp500_research_agent.MCPBackend",
        side_effect=_factory,
    ):
        with patch("demos.research.run_sp500_research_agent.shutil.which", return_value="/usr/bin/fake"):
            kept = await _filter_mcp_server_configs([cfg], timeout_seconds=1.0)
    assert kept == []


@pytest.mark.asyncio
async def test_filter_mcp_server_configs_skips_missing_stdio_command() -> None:
    cfg = {"transport": "stdio", "command": "not-a-real-binary"}
    with patch("demos.research.run_sp500_research_agent.shutil.which", return_value=None):
        with patch("demos.research.run_sp500_research_agent.MCPBackend") as backend_cls:
            kept = await _filter_mcp_server_configs([cfg], timeout_seconds=1.0)
    assert kept == []
    backend_cls.assert_not_called()
