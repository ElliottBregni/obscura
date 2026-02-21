"""Shared runtime helpers for backend-specific demo agents."""

from __future__ import annotations

from dataclasses import dataclass

from sdk.agent.agents import AgentRuntime
from sdk.auth.models import AuthenticatedUser


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
    import asyncio

    runtime = AgentRuntime(user=make_demo_user(config.role))

    try:
        try:
            await asyncio.wait_for(runtime.start(), timeout=start_timeout_seconds)
        except TimeoutError as exc:
            raise TimeoutError(
                f"Timed out starting runtime after {start_timeout_seconds}s."
            ) from exc

        agent = runtime.spawn(
            config.name,
            model=config.backend_model,
            system_prompt=config.system_prompt,
            memory_namespace=config.memory_namespace,
        )
        agent.heartbeat_enabled = False
        try:
            await asyncio.wait_for(agent.start(), timeout=start_timeout_seconds)
        except TimeoutError as exc:
            raise TimeoutError(
                "Timed out starting backend agent. "
                f"backend={config.backend_model} timeout={start_timeout_seconds}s"
            ) from exc

        if stream:
            async def _collect_stream() -> str:
                chunks: list[str] = []
                async for chunk in agent.stream(prompt):
                    chunks.append(chunk)
                return "".join(chunks)

            try:
                return await asyncio.wait_for(
                    _collect_stream(),
                    timeout=run_timeout_seconds,
                )
            except TimeoutError as exc:
                raise TimeoutError(
                    "Timed out waiting for streaming response. "
                    f"backend={config.backend_model} timeout={run_timeout_seconds}s"
                ) from exc

        try:
            result = await asyncio.wait_for(
                agent.run(prompt),
                timeout=run_timeout_seconds,
            )
        except TimeoutError as exc:
            raise TimeoutError(
                "Timed out waiting for response. "
                f"backend={config.backend_model} timeout={run_timeout_seconds}s"
            ) from exc
        return str(result)
    finally:
        await runtime.stop()
