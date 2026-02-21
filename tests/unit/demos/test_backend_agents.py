"""Tests for demos.backend_agents runnable Claude/Codex agents."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import MagicMock, mock_open, patch

import pytest

from demos.backend_agents.common import BackendAgentConfig, run_backend_agent
from demos.backend_agents.run_claude_agent import build_parser as build_claude_parser
from demos.backend_agents.run_copilot_agent import (
    build_parser as build_copilot_parser,
    run_copilot_cli_oauth,
)
from demos.backend_agents.run_codex_agent import (
    build_parser as build_codex_parser,
    run_codex_cli_oauth,
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
        yield f"stream:{self.model}:"
        yield prompt


class _FakeRuntime:
    last_instance: _FakeRuntime | None = None

    def __init__(self, user: Any) -> None:
        self.user = user
        self.spawn_calls: list[dict[str, Any]] = []
        self.started = False
        self.stopped = False
        _FakeRuntime.last_instance = self

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    def spawn(
        self,
        name: str,
        model: str = "copilot",
        system_prompt: str = "",
        memory_namespace: str = "default",
        **_: Any,
    ) -> _FakeAgent:
        self.spawn_calls.append(
            {
                "name": name,
                "model": model,
                "system_prompt": system_prompt,
                "memory_namespace": memory_namespace,
            }
        )
        return _FakeAgent(model=model)


class _SlowStartRuntime(_FakeRuntime):
    async def start(self) -> None:
        import asyncio

        await asyncio.sleep(0.2)


class TestRunBackendAgent:
    @pytest.mark.asyncio
    async def test_non_stream_claude_agent(self) -> None:
        config = BackendAgentConfig(
            name="claude-test-agent",
            backend_model="claude",
            role="agent:claude",
            system_prompt="Claude prompt",
            memory_namespace="demo:claude",
        )

        with patch("demos.backend_agents.common.AgentRuntime", _FakeRuntime):
            result = await run_backend_agent(config, "hello", stream=False)

        assert result == "run:claude:hello"
        runtime = _FakeRuntime.last_instance
        assert runtime is not None
        assert runtime.started is True
        assert runtime.stopped is True
        assert runtime.spawn_calls[0]["model"] == "claude"
        assert runtime.spawn_calls[0]["memory_namespace"] == "demo:claude"
        assert "agent:claude" in runtime.user.roles

    @pytest.mark.asyncio
    async def test_stream_openai_agent(self) -> None:
        config = BackendAgentConfig(
            name="codex-test-agent",
            backend_model="openai",
            role="agent:openai",
            system_prompt="Codex prompt",
            memory_namespace="demo:codex",
        )

        with patch("demos.backend_agents.common.AgentRuntime", _FakeRuntime):
            result = await run_backend_agent(config, "stream this", stream=True)

        assert result == "stream:openai:stream this"
        runtime = _FakeRuntime.last_instance
        assert runtime is not None
        assert runtime.spawn_calls[0]["model"] == "openai"
        assert "agent:openai" in runtime.user.roles

    @pytest.mark.asyncio
    async def test_start_timeout(self) -> None:
        config = BackendAgentConfig(
            name="claude-test-agent",
            backend_model="claude",
            role="agent:claude",
            system_prompt="Claude prompt",
            memory_namespace="demo:claude",
        )

        with patch("demos.backend_agents.common.AgentRuntime", _SlowStartRuntime):
            with pytest.raises(TimeoutError, match="Timed out starting runtime"):
                await run_backend_agent(
                    config,
                    "hello",
                    start_timeout_seconds=0.01,
                )


class TestBackendAgentCLI:
    def test_claude_parser_defaults(self) -> None:
        args = build_claude_parser().parse_args([])
        assert args.stream is False
        assert isinstance(args.prompt, str)
        assert args.prompt
        assert args.start_timeout == 20.0
        assert args.run_timeout == 120.0

    def test_codex_parser_defaults(self) -> None:
        args = build_codex_parser().parse_args([])
        assert args.stream is False
        assert isinstance(args.prompt, str)
        assert args.prompt
        assert args.start_timeout == 20.0
        assert args.run_timeout == 120.0
        assert args.no_cli_fallback is False
        assert args.sdk_first is False

    def test_copilot_parser_defaults(self) -> None:
        args = build_copilot_parser().parse_args([])
        assert args.stream is False
        assert isinstance(args.prompt, str)
        assert args.prompt
        assert args.start_timeout == 20.0
        assert args.run_timeout == 120.0
        assert args.sdk_first is False
        assert args.no_cli_fallback is False

    def test_run_codex_cli_oauth_success(self) -> None:
        status = MagicMock()
        status.returncode = 0
        status.stdout = "Logged in using ChatGPT\n"
        status.stderr = ""

        execute = MagicMock()
        execute.returncode = 0
        execute.stdout = ""
        execute.stderr = ""

        class _Tmp:
            name = "/tmp/codex-last.txt"

            def close(self) -> None:
                return None

        with patch("subprocess.run", side_effect=[status, execute]):
            with patch("tempfile.NamedTemporaryFile", return_value=_Tmp()):
                with patch("os.path.exists", return_value=True):
                    with patch("builtins.open", mock_open(read_data="final message")):
                        with patch("os.unlink"):
                            out = run_codex_cli_oauth("hello", timeout_seconds=10)
                            assert out == "final message"

    def test_run_codex_cli_oauth_not_logged_in(self) -> None:
        status = MagicMock()
        status.returncode = 0
        status.stdout = "Not logged in\n"
        status.stderr = ""

        with patch("subprocess.run", return_value=status):
            with pytest.raises(RuntimeError, match="not logged in"):
                run_codex_cli_oauth("hello", timeout_seconds=10)

    def test_run_copilot_cli_oauth_success(self) -> None:
        proc = MagicMock()
        proc.returncode = 0
        proc.stdout = "copilot reply\n"
        proc.stderr = ""
        with patch("subprocess.run", return_value=proc):
            out = run_copilot_cli_oauth("hello", timeout_seconds=10)
            assert out == "copilot reply"

    def test_run_copilot_cli_oauth_error(self) -> None:
        proc = MagicMock()
        proc.returncode = 1
        proc.stdout = ""
        proc.stderr = "auth failed"
        with patch("subprocess.run", return_value=proc):
            with pytest.raises(RuntimeError, match="copilot CLI failed"):
                run_copilot_cli_oauth("hello", timeout_seconds=10)
