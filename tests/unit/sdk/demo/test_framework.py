"""Tests for sdk.demo.framework."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from sdk.demo.framework import (
    DemoAgentConfig,
    make_demo_user,
    required_args_tool_guard,
    run_demo_prompt,
)
from sdk.internal.types import ToolCallInfo, ToolSpec


class _FakeAgent:
    def __init__(self, model: str) -> None:
        self.model = model
        self.heartbeat_enabled = True
        self._tools = [
            ToolSpec(
                name="run_shell",
                description="demo",
                parameters={"type": "object", "required": ["script"]},
                handler=lambda: "",
                required_tier="privileged",
            )
        ]

    async def start(self) -> None:
        return None

    async def run(self, prompt: str) -> str:
        return f"run:{self.model}:{prompt}"

    async def stream(self, prompt: str) -> AsyncIterator[str]:
        yield f"stream:{self.model}:"
        yield prompt

    async def run_loop(
        self,
        prompt: str,
        *,
        max_turns: int = 8,
        on_confirm: Any = None,
    ) -> str:
        _ = max_turns
        # emulate missing args call being denied
        if on_confirm is not None:
            approved = on_confirm(ToolCallInfo(tool_use_id="1", name="run_shell", input={}))
            if approved is False:
                return f"loop-denied:{self.model}:{prompt}"
        return f"loop:{self.model}:{prompt}"

    def list_registered_tools(self) -> list[ToolSpec]:
        return self._tools


class _FakeRuntime:
    def __init__(self, user: Any) -> None:
        self.user = user
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    def spawn(
        self,
        name: str,
        model: str,
        system_prompt: str,
        memory_namespace: str,
        **kwargs: Any,
    ) -> _FakeAgent:
        _ = name
        _ = system_prompt
        _ = memory_namespace
        _ = kwargs
        return _FakeAgent(model)


def test_make_demo_user_role() -> None:
    user = make_demo_user("agent:claude")
    assert "agent:claude" in user.roles
    assert "operator" in user.roles


@pytest.mark.asyncio
async def test_run_demo_prompt_stream() -> None:
    config = DemoAgentConfig(
        name="demo",
        model="copilot",
        role="agent:copilot",
        system_prompt="x",
        memory_namespace="demo:x",
    )
    result = await run_demo_prompt(
        config,
        "hello",
        stream=True,
        runtime_cls=_FakeRuntime,  # type: ignore[arg-type]
    )
    assert result == "stream:copilot:hello"


def test_required_args_guard_rejects_missing_required_input() -> None:
    guard = required_args_tool_guard(_FakeAgent("copilot"))  # type: ignore[arg-type]
    ok = guard(ToolCallInfo(tool_use_id="1", name="run_shell", input={}))
    assert ok is False
