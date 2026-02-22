"""Tests for demos.backend_agents.run_live_system_tools_agent."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from unittest.mock import MagicMock, patch

import pytest

from demos.backend_agents.run_live_system_tools_agent import (
    build_parser,
    run_live_system_tools_demo,
)
from obscura.core.types import AgentEvent, AgentEventKind


async def _fake_events() -> AsyncIterator[AgentEvent]:
    yield AgentEvent(kind=AgentEventKind.TOOL_CALL, tool_name="list_system_tools")
    yield AgentEvent(
        kind=AgentEventKind.TEXT_DELTA,
        text="done",
    )
    yield AgentEvent(kind=AgentEventKind.AGENT_DONE)


@pytest.mark.asyncio
async def test_run_live_system_tools_demo_collects_events() -> None:
    agent = MagicMock()
    agent.list_registered_tools = MagicMock(return_value=[])
    agent.stream_loop = MagicMock(return_value=_fake_events())

    @asynccontextmanager
    async def _fake_session(*args: object, **kwargs: object) -> AsyncIterator[MagicMock]:
        _ = args
        _ = kwargs
        yield agent

    with patch(
        "demos.backend_agents.run_live_system_tools_agent.demo_agent_session",
        _fake_session,
    ):
        text, calls = await run_live_system_tools_demo(
            backend="copilot",
            prompt="test",
            show_events=False,
        )

    assert text == "done"
    assert calls == ["list_system_tools({})"]


def test_parser_accepts_backend_and_prompt() -> None:
    parser = build_parser()
    args = parser.parse_args(["--backend", "claude", "--prompt", "hi", "--max-turns", "3"])
    assert args.backend == "claude"
    assert args.prompt == "hi"
    assert args.max_turns == 3
