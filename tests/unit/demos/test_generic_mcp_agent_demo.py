"""Tests for demos.mcp.run_generic_mcp_agent."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, override
from unittest.mock import patch

import pytest

from demos.mcp.run_generic_mcp_agent import (
    add_server,
    build_agent_servers,
    build_parser,
    discover_servers,
    load_mcp_config,
    run_mcp_agent,
)


class _FakeAgent:
    def __init__(self, model: str) -> None:
        self.model = model
        self.heartbeat_enabled = True

    async def start(self) -> None:
        return None

    async def run(self, prompt: str) -> str:
        return f"run:{self.model}:{prompt}"

    async def stream(self, prompt: str) -> AsyncIterator[str]:
        yield "stream:"
        yield prompt


class _FakeRuntime:
    last_instance: _FakeRuntime | None = None

    def __init__(self, user: Any) -> None:
        self.user = user
        self.started = False
        self.stopped = False
        self.spawn_calls: list[dict[str, Any]] = []
        _FakeRuntime.last_instance = self

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    def spawn(self, *args: Any, **kwargs: Any) -> _FakeAgent:
        if args:
            kwargs["name"] = args[0]
        self.spawn_calls.append(kwargs)
        return _FakeAgent(model=str(kwargs.get("model", "claude")))


class _SlowRuntime(_FakeRuntime):
    @override
    async def start(self) -> None:
        import asyncio

        await asyncio.sleep(0.2)


class TestGenericMCPDiscovery:
    def test_add_and_discover_servers(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg_path = tmp_path / "mcp-config.json"
        add_server(
            path=cfg_path,
            name="playwright",
            transport="stdio",
            command="npx",
            args=("-y", "@playwright/mcp@latest"),
            url="",
            env={"PW_TOKEN": "${PW_TOKEN}"},
        )

        monkeypatch.setenv("PW_TOKEN", "token-value")
        servers = discover_servers(cfg_path)
        assert len(servers) == 1
        s = servers[0]
        assert s.name == "playwright"
        assert s.command == "npx"
        assert s.missing_env == ()
        assert s.env["PW_TOKEN"] == "token-value"

    def test_build_agent_servers_selected(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "mcp-config.json"
        add_server(
            path=cfg_path,
            name="one",
            transport="stdio",
            command="cmd1",
            args=(),
            url="",
            env={},
        )
        add_server(
            path=cfg_path,
            name="two",
            transport="sse",
            command="",
            args=(),
            url="http://mcp.local/sse",
            env={},
        )
        discovered = discover_servers(cfg_path)
        servers = build_agent_servers(discovered, {"two"})
        assert len(servers) == 1
        assert servers[0]["transport"] == "sse"
        assert servers[0]["url"] == "http://mcp.local/sse"

    def test_load_defaults_when_missing(self, tmp_path: Path) -> None:
        cfg = load_mcp_config(tmp_path / "missing.json")
        assert "mcpServers" in cfg


class TestGenericMCPRun:
    @pytest.mark.asyncio
    async def test_run_non_stream(self) -> None:
        with patch("demos.mcp.run_generic_mcp_agent.AgentRuntime", _FakeRuntime):
            out = await run_mcp_agent(
                model="claude",
                prompt="use tools",
                servers=[{"transport": "stdio", "command": "npx", "args": []}],
                stream=False,
                start_timeout_seconds=1.0,
                run_timeout_seconds=1.0,
            )
        assert out == "run:claude:use tools"
        runtime = _FakeRuntime.last_instance
        assert runtime is not None
        mcp_cfg = runtime.spawn_calls[0]["mcp"]
        assert mcp_cfg.enabled is True
        assert len(mcp_cfg.servers) == 1

    @pytest.mark.asyncio
    async def test_run_stream(self) -> None:
        with patch("demos.mcp.run_generic_mcp_agent.AgentRuntime", _FakeRuntime):
            out = await run_mcp_agent(
                model="claude",
                prompt="tool list",
                servers=[{"transport": "stdio", "command": "npx", "args": []}],
                stream=True,
                start_timeout_seconds=1.0,
                run_timeout_seconds=1.0,
            )
        assert out == "stream:tool list"

    @pytest.mark.asyncio
    async def test_run_start_timeout(self) -> None:
        with patch("demos.mcp.run_generic_mcp_agent.AgentRuntime", _SlowRuntime):
            with pytest.raises(TimeoutError):
                await run_mcp_agent(
                    model="claude",
                    prompt="x",
                    servers=[{"transport": "stdio", "command": "npx", "args": []}],
                    stream=False,
                    start_timeout_seconds=0.01,
                    run_timeout_seconds=1.0,
                )


class TestGenericMCPParser:
    def test_parser_modes(self) -> None:
        parser = build_parser()
        args_discover = parser.parse_args(["discover"])
        assert args_discover.command == "discover"
        args_add = parser.parse_args(["add", "--name", "playwright"])
        assert args_add.command == "add"
        args_run = parser.parse_args(["run", "--all"])
        assert args_run.command == "run"
