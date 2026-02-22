"""Clean template for creating an Obscura instance and a managed agent."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from typing import Literal

from obscura import ObscuraClient
from obscura.agent.agents import AgentRuntime
from obscura.auth.models import AuthenticatedUser


BackendName = Literal["copilot", "claude", "openai", "moonshot", "localllm"]


@dataclass(frozen=True)
class ObscuraInstanceConfig:
    """Config for direct ObscuraClient usage."""

    backend: BackendName = "copilot"
    model: str | None = None
    system_prompt: str = "You are a helpful assistant."
    timeout_seconds: float = 120.0


@dataclass(frozen=True)
class ManagedAgentConfig:
    """Config for runtime-managed agent usage."""

    name: str = "template-agent"
    backend: BackendName = "copilot"
    system_prompt: str = "You are a focused agent that returns concise answers."
    memory_namespace: str = "template:agent"
    timeout_seconds: float = 180.0
    max_iterations: int = 8


def make_user_for_backend(backend: BackendName) -> AuthenticatedUser:
    """Create a deterministic user identity for local template runs."""
    role = f"agent:{backend}"
    return AuthenticatedUser(
        user_id=f"template-{backend}-user",
        email=f"{backend}@obscura.local",
        roles=("operator", role),
        org_id="org-template",
        token_type="user",
        raw_token="template-token",
    )


async def run_obscura_instance(
    config: ObscuraInstanceConfig,
    prompt: str,
) -> str:
    """Use ObscuraClient directly (no AgentRuntime)."""
    async with ObscuraClient(
        config.backend,
        model=config.model,
        system_prompt=config.system_prompt,
    ) as client:
        message = await asyncio.wait_for(client.send(prompt), timeout=config.timeout_seconds)
    return message.text


async def run_managed_agent(
    config: ManagedAgentConfig,
    prompt: str,
) -> str:
    """Use AgentRuntime + spawned agent (recommended for workflows)."""
    runtime = AgentRuntime(user=make_user_for_backend(config.backend))
    try:
        await runtime.start()
        agent = runtime.spawn(
            name=config.name,
            model=config.backend,
            system_prompt=config.system_prompt,
            memory_namespace=config.memory_namespace,
            max_iterations=config.max_iterations,
            timeout_seconds=config.timeout_seconds,
            enable_system_tools=True,
        )
        agent.heartbeat_enabled = False
        await agent.start()
        result = await agent.run(prompt)
        return str(result)
    finally:
        await runtime.stop()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Obscura clean agent/instance template")
    parser.add_argument(
        "--mode",
        choices=("instance", "agent"),
        default="agent",
        help="Use direct client instance or runtime-managed agent.",
    )
    parser.add_argument(
        "--backend",
        choices=("copilot", "claude", "openai", "moonshot", "localllm"),
        default="copilot",
        help="Backend to run.",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Optional model ID override (instance mode only).",
    )
    parser.add_argument(
        "--prompt",
        "-p",
        default="Give me a 3-bullet summary of what you can do.",
        help="Prompt to run.",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    backend = args.backend
    prompt = args.prompt
    if args.mode == "instance":
        config = ObscuraInstanceConfig(
            backend=backend,
            model=args.model,
        )
        text = asyncio.run(run_obscura_instance(config, prompt))
    else:
        config = ManagedAgentConfig(
            name=f"{backend}-template-agent",
            backend=backend,
        )
        text = asyncio.run(run_managed_agent(config, prompt))
    print(text)


if __name__ == "__main__":
    main()
