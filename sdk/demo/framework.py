"""SDK-level framework for running demos with minimal script logic."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from sdk.agent.agents import Agent, AgentRuntime
from sdk.auth.models import AuthenticatedUser
from sdk.internal.types import ToolCallInfo


@dataclass(frozen=True)
class DemoAgentConfig:
    """Configuration for a demo agent session."""

    name: str
    model: str
    role: str
    system_prompt: str
    memory_namespace: str
    enable_system_tools: bool = False


ToolConfirmGuard = Callable[[ToolCallInfo], bool | Awaitable[bool]]


def make_demo_user(role: str) -> AuthenticatedUser:
    """Create a deterministic demo user scoped to one role."""
    return AuthenticatedUser(
        user_id=f"demo-{role}-user",
        email=f"{role}@obscura.dev",
        roles=("operator", role),
        org_id="org-demo",
        token_type="user",
        raw_token="demo-token",
    )


@asynccontextmanager
async def demo_agent_session(
    config: DemoAgentConfig,
    *,
    user: AuthenticatedUser | None = None,
    runtime_cls: type[AgentRuntime] = AgentRuntime,
    start_timeout_seconds: float = 20.0,
    spawn_kwargs: dict[str, Any] | None = None,
) -> AsyncIterator[Agent]:
    """Create/start a demo runtime+agent and always clean up."""
    runtime = runtime_cls(user=user or make_demo_user(config.role))
    try:
        try:
            await asyncio.wait_for(runtime.start(), timeout=start_timeout_seconds)
        except TimeoutError as exc:
            raise TimeoutError(
                f"Timed out starting runtime after {start_timeout_seconds}s."
            ) from exc

        agent = runtime.spawn(
            config.name,
            model=config.model,
            system_prompt=config.system_prompt,
            memory_namespace=config.memory_namespace,
            enable_system_tools=config.enable_system_tools,
            **(spawn_kwargs or {}),
        )
        agent.heartbeat_enabled = False
        try:
            await asyncio.wait_for(agent.start(), timeout=start_timeout_seconds)
        except TimeoutError as exc:
            raise TimeoutError(
                "Timed out starting backend agent. "
                f"backend={config.model} timeout={start_timeout_seconds}s"
            ) from exc
        yield agent
    finally:
        await runtime.stop()


async def collect_stream_text(stream: AsyncIterator[str]) -> str:
    """Collect streamed text into one string."""
    chunks: list[str] = []
    async for chunk in stream:
        chunks.append(chunk)
    return "".join(chunks)


async def run_demo_prompt(
    config: DemoAgentConfig,
    prompt: str,
    *,
    stream: bool = False,
    use_loop: bool = False,
    on_confirm: ToolConfirmGuard | None = None,
    user: AuthenticatedUser | None = None,
    runtime_cls: type[AgentRuntime] = AgentRuntime,
    start_timeout_seconds: float = 20.0,
    run_timeout_seconds: float = 120.0,
    spawn_kwargs: dict[str, Any] | None = None,
) -> str:
    """Run prompt against a demo agent and return text output."""
    async with demo_agent_session(
        config,
        user=user,
        runtime_cls=runtime_cls,
        start_timeout_seconds=start_timeout_seconds,
        spawn_kwargs=spawn_kwargs,
    ) as agent:
        if use_loop:
            try:
                return await asyncio.wait_for(
                    agent.run_loop(
                        prompt,
                        max_turns=8,
                        on_confirm=on_confirm,
                    ),
                    timeout=run_timeout_seconds,
                )
            except TimeoutError as exc:
                raise TimeoutError(
                    "Timed out waiting for loop response. "
                    f"backend={config.model} timeout={run_timeout_seconds}s"
                ) from exc

        if stream:
            try:
                return await asyncio.wait_for(
                    collect_stream_text(agent.stream(prompt)),
                    timeout=run_timeout_seconds,
                )
            except TimeoutError as exc:
                raise TimeoutError(
                    "Timed out waiting for streaming response. "
                    f"backend={config.model} timeout={run_timeout_seconds}s"
                ) from exc

        try:
            result = await asyncio.wait_for(
                agent.run(prompt),
                timeout=run_timeout_seconds,
            )
        except TimeoutError as exc:
            raise TimeoutError(
                "Timed out waiting for response. "
                f"backend={config.model} timeout={run_timeout_seconds}s"
            ) from exc
        return str(result)


def required_args_tool_guard(agent: Agent) -> ToolConfirmGuard:
    """Return a guard that rejects tool calls missing required JSON inputs."""
    required_by_tool: dict[str, list[str]] = {}
    for spec in agent.list_registered_tools():
        required_raw = spec.parameters.get("required", [])
        required_fields = [
            str(name) for name in required_raw if isinstance(name, str)
        ]
        required_by_tool[spec.name] = required_fields

    def _confirm(call: ToolCallInfo) -> bool:
        required_fields = required_by_tool.get(call.name, [])
        for field in required_fields:
            value = call.input.get(field)
            if value is None:
                return False
            if isinstance(value, str) and not value.strip():
                return False
            if isinstance(value, list) and value == []:
                return False
            if isinstance(value, dict) and value == {}:
                return False
        return True

    return _confirm
