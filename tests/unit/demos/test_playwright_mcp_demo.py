"""Tests for demos.mcp.run_playwright_mcp_demo."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, override
from unittest.mock import patch

import pytest

from demos.mcp.run_playwright_mcp_demo import (
    PlaywrightMCPDemoConfig,
    build_parser,
    parse_env_json,
    run_playwright_mcp_demo,
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

    def spawn(
        self,
        name: str,
        model: str = "claude",
        system_prompt: str = "",
        memory_namespace: str = "default",
        **kwargs: Any,
    ) -> _FakeAgent:
        self.spawn_calls.append(
            {
                "name": name,
                "model": model,
                "system_prompt": system_prompt,
                "memory_namespace": memory_namespace,
                "mcp": kwargs.get("mcp"),
            }
        )
        return _FakeAgent(model=model)


class _SlowRuntime(_FakeRuntime):
    @override
    async def start(self) -> None:
        import asyncio

        await asyncio.sleep(0.2)


class TestPlaywrightMCPDemo:
    @pytest.mark.asyncio
    async def test_run_demo_non_stream_wires_mcp_config(self) -> None:
        cfg = PlaywrightMCPDemoConfig(
            model="claude",
            prompt="open example.com",
            stream=False,
            start_timeout_seconds=1.0,
            run_timeout_seconds=1.0,
            mcp_command="npx",
            mcp_args=("-y", "@playwright/mcp@latest"),
            mcp_env={"DEBUG": "pw:mcp"},
        )
        with patch("demos.mcp.run_playwright_mcp_demo.AgentRuntime", _FakeRuntime):
            result = await run_playwright_mcp_demo(cfg)

        assert result == "run:claude:open example.com"
        runtime = _FakeRuntime.last_instance
        assert runtime is not None
        assert runtime.started is True
        assert runtime.stopped is True
        call = runtime.spawn_calls[0]
        assert call["model"] == "claude"
        assert call["memory_namespace"] == "demo:playwright:mcp"
        mcp_cfg = call["mcp"]
        assert mcp_cfg.enabled is True
        assert len(mcp_cfg.servers) == 1
        assert mcp_cfg.servers[0]["command"] == "npx"
        assert mcp_cfg.servers[0]["args"] == ["-y", "@playwright/mcp@latest"]

    @pytest.mark.asyncio
    async def test_run_demo_stream(self) -> None:
        cfg = PlaywrightMCPDemoConfig(
            model="claude",
            prompt="stream it",
            stream=True,
            start_timeout_seconds=1.0,
            run_timeout_seconds=1.0,
            mcp_command="npx",
            mcp_args=("-y", "@playwright/mcp@latest"),
            mcp_env={},
        )
        with patch("demos.mcp.run_playwright_mcp_demo.AgentRuntime", _FakeRuntime):
            result = await run_playwright_mcp_demo(cfg)
        assert result == "stream:stream it"

    @pytest.mark.asyncio
    async def test_run_demo_start_timeout(self) -> None:
        cfg = PlaywrightMCPDemoConfig(
            model="claude",
            prompt="x",
            stream=False,
            start_timeout_seconds=0.01,
            run_timeout_seconds=1.0,
            mcp_command="npx",
            mcp_args=("-y", "@playwright/mcp@latest"),
            mcp_env={},
        )
        with patch("demos.mcp.run_playwright_mcp_demo.AgentRuntime", _SlowRuntime):
            with pytest.raises(TimeoutError):
                await run_playwright_mcp_demo(cfg)


class TestPlaywrightMCPParser:
    def test_parser_defaults(self) -> None:
        args = build_parser().parse_args([])
        assert args.model == "claude"
        assert args.stream is False
        assert args.start_timeout == 30.0
        assert args.run_timeout == 180.0
        assert args.mcp_command == "npx"

    def test_parse_env_json(self) -> None:
        env = parse_env_json('{"DEBUG":"pw:mcp","HEADLESS":true}')
        assert env["DEBUG"] == "pw:mcp"
        assert env["HEADLESS"] == "True"

    def test_parse_env_json_invalid(self) -> None:
        with pytest.raises(ValueError, match="Invalid --mcp-env JSON"):
            parse_env_json("{")
