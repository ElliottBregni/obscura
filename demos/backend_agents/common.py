"""Shared runtime helpers for backend-specific demo agents."""

from __future__ import annotations

from dataclasses import dataclass

from obscura.agent.agents import AgentRuntime
from obscura.auth.models import AuthenticatedUser
from obscura.demo.framework import DemoAgentConfig, make_demo_user, run_demo_prompt


@dataclass(frozen=True)
class BackendAgentConfig:
    """Configuration for a backend-specific demo agent."""

    name: str
    backend_model: str
    role: str
    system_prompt: str
    memory_namespace: str


def make_demo_user(role: str) -> AuthenticatedUser:
    """Create a deterministic demo user scoped to one backend role."""
    return AuthenticatedUser(
        user_id=f"demo-{role}-user",
        email=f"{role}@obscura.dev",
        roles=("operator", role),
        org_id="org-demo",
        token_type="user",
        raw_token="demo-token",
    )


async def run_backend_agent(
    config: BackendAgentConfig,
    prompt: str,
    *,
    stream: bool = False,
    start_timeout_seconds: float = 20.0,
    run_timeout_seconds: float = 120.0,
) -> str:
    """Run one backend-specific agent task and return text output."""
    demo_config = DemoAgentConfig(
        name=config.name,
        model=config.backend_model,
        role=config.role,
        system_prompt=config.system_prompt,
        memory_namespace=config.memory_namespace,
    )
    return await run_demo_prompt(
        demo_config,
        prompt,
        stream=stream,
        user=make_demo_user(config.role),
        runtime_cls=AgentRuntime,
        start_timeout_seconds=start_timeout_seconds,
        run_timeout_seconds=run_timeout_seconds,
    )
