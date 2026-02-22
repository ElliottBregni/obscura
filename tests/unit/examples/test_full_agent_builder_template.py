"""Tests for examples.full_agent_builder_template."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from examples.full_agent_builder_template import (
    APERProfile,
    AgentBuilder,
    build_parser,
)
from obscura.core.types import AgentEvent, AgentEventKind


class _FakeAgent:
    def __init__(self) -> None:
        self.heartbeat_enabled = True

    async def start(self) -> None:
        return None

    async def run(self, prompt: str) -> str:
        return f"run:{prompt}"

    async def stream(self, prompt: str) -> AsyncIterator[str]:
        yield "stream:"
        yield prompt

    async def run_loop(self, prompt: str, *, max_turns: int | None = None) -> str:
        _ = max_turns
        return f"loop:{prompt}"

    async def stream_loop(
        self, prompt: str, *, max_turns: int | None = None
    ) -> AsyncIterator[AgentEvent]:
        _ = max_turns
        yield AgentEvent(kind=AgentEventKind.TEXT_DELTA, text=f"sl:{prompt}")
        yield AgentEvent(kind=AgentEventKind.AGENT_DONE)


class _FakeRuntime:
    last_spawn_kwargs: dict[str, Any] | None = None

    def __init__(self, user: Any) -> None:
        self.user = user
        self._agent = _FakeAgent()

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    def spawn(self, **kwargs: Any) -> _FakeAgent:
        _FakeRuntime.last_spawn_kwargs = kwargs
        return self._agent


class _FakeClient:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        _ = args
        _ = kwargs
        self.register_tool = MagicMock()

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        _ = exc
        return None

    async def run_loop_to_completion(
        self,
        prompt: str,
        *,
        max_turns: int = 8,
    ) -> str:
        _ = max_turns
        return f"aper-exec:{prompt[:24]}"


class TestBuilderTemplate:
    def test_parser_accepts_aper_mode(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["--mode", "aper", "--backend", "claude"])
        assert args.mode == "aper"
        assert args.backend == "claude"

    def test_build_spawn_kwargs_includes_mcp_and_a2a(self) -> None:
        builder = (
            AgentBuilder()
            .with_identity(name="x", backend="claude", memory_namespace="m")
            .with_mcp_stdio_server(name="fs", command="npx", args=["-y", "mcp-fs"])
            .with_a2a_remote_tools(urls=["http://a2a.local"])
        )
        kwargs = builder.build_spawn_kwargs()
        assert kwargs["name"] == "x"
        assert kwargs["model"] == "claude"
        assert kwargs["mcp"].enabled is True
        assert kwargs["a2a_remote_tools"]["enabled"] is True

    @pytest.mark.asyncio
    async def test_run_loop_mode_uses_runtime_agent(self) -> None:
        builder = AgentBuilder().with_identity(backend="copilot")
        with patch("examples.full_agent_builder_template.AgentRuntime", _FakeRuntime):
            out = await builder.run("hello", mode="loop")
        assert out == "loop:hello"
        assert _FakeRuntime.last_spawn_kwargs is not None
        assert _FakeRuntime.last_spawn_kwargs["model"] == "copilot"

    @pytest.mark.asyncio
    async def test_run_aper_mode_uses_custom_aper_agent(self) -> None:
        builder = AgentBuilder().with_identity(name="aper-agent", backend="claude")
        builder.with_aper_profile(APERProfile(max_turns=5))
        with patch("examples.full_agent_builder_template.ObscuraClient", _FakeClient):
            out = await builder.run("investigate", mode="aper")
        assert "Execution Output:" in out
        assert "aper-exec:" in out
