"""Example: run full agent builder with two different custom APER profiles."""

from __future__ import annotations

import argparse
import asyncio
from typing import Literal, cast

try:
    from examples.full_agent_builder_template import (
        APERProfile,
        AgentBuilder,
        BackendName,
    )
except ModuleNotFoundError:
    # Support direct file execution:
    # `uv run python examples/full_agent_builder_two_profiles.py ...`
    from full_agent_builder_template import APERProfile, AgentBuilder, BackendName


ProfileName = Literal["fast", "deep", "both"]


FAST_APER_PROFILE = APERProfile(
    analyze_template="Extract the goal quickly and list only critical constraints.",
    plan_template="Create a minimal plan with at most 3 steps.",
    execute_template=(
        "Goal:\n{goal}\n\n"
        "Analysis:\n{analysis}\n\n"
        "Plan:\n{plan}\n\n"
        "Execute quickly. Prefer lightweight checks and short outputs."
    ),
    respond_template="Provide a short actionable summary.",
    max_turns=4,
)


DEEP_APER_PROFILE = APERProfile(
    analyze_template="Perform deep analysis, risks, and edge cases before acting.",
    plan_template="Produce a detailed plan with validation steps and tradeoffs.",
    execute_template=(
        "Goal:\n{goal}\n\n"
        "Analysis:\n{analysis}\n\n"
        "Plan:\n{plan}\n\n"
        "Execute thoroughly. Use tools to validate assumptions and cite evidence."
    ),
    respond_template=(
        "Return a structured response with findings, risks, and recommended next steps."
    ),
    max_turns=10,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run two custom APER profiles using full agent builder template."
    )
    parser.add_argument(
        "--backend",
        choices=("copilot", "claude", "openai", "moonshot", "localllm"),
        default="copilot",
        help="Backend to run.",
    )
    parser.add_argument(
        "--profile",
        choices=("fast", "deep", "both"),
        default="both",
        help="Which APER profile to run.",
    )
    parser.add_argument(
        "--prompt",
        "-p",
        default="Analyze this repository and propose an implementation plan.",
        help="Prompt to run through APER.",
    )
    parser.add_argument(
        "--mcp-discover",
        action="store_true",
        help="Enable MCP auto-discovery from config.",
    )
    parser.add_argument(
        "--mcp-config",
        default="config/mcp-config.json",
        help="MCP config path when --mcp-discover is enabled.",
    )
    parser.add_argument(
        "--mcp-server-names",
        default="",
        help="Comma-separated MCP server names for discovery filter.",
    )
    return parser


def _build_profiled_builder(
    *,
    backend: BackendName,
    profile_name: Literal["fast", "deep"],
    enable_mcp_discover: bool,
    mcp_config: str,
    mcp_server_names: list[str],
) -> AgentBuilder:
    profile = FAST_APER_PROFILE if profile_name == "fast" else DEEP_APER_PROFILE
    builder = (
        AgentBuilder()
        .with_identity(
            name=f"{backend}-{profile_name}-aper-agent",
            backend=backend,
            memory_namespace=f"examples:aper:{backend}:{profile_name}",
        )
        .with_runtime_options(
            enable_system_tools=True,
            tags=["example", "aper", profile_name],
        )
        .with_aper_profile(profile)
    )
    if enable_mcp_discover:
        builder.with_mcp_discovery(
            config_path=mcp_config,
            server_names=mcp_server_names,
        )
    return builder


async def run_profile(
    *,
    backend: BackendName,
    profile_name: Literal["fast", "deep"],
    prompt: str,
    enable_mcp_discover: bool,
    mcp_config: str,
    mcp_server_names: list[str],
) -> str:
    builder = _build_profiled_builder(
        backend=backend,
        profile_name=profile_name,
        enable_mcp_discover=enable_mcp_discover,
        mcp_config=mcp_config,
        mcp_server_names=mcp_server_names,
    )
    return await builder.run(prompt, mode="aper")


async def run_selected_profiles(
    *,
    backend: BackendName,
    profile: ProfileName,
    prompt: str,
    enable_mcp_discover: bool,
    mcp_config: str,
    mcp_server_names: list[str],
) -> list[tuple[str, str]]:
    outputs: list[tuple[str, str]] = []
    selected: list[Literal["fast", "deep"]]
    if profile == "both":
        selected = ["fast", "deep"]
    elif profile == "fast":
        selected = ["fast"]
    else:
        selected = ["deep"]

    for item in selected:
        text = await run_profile(
            backend=backend,
            profile_name=item,
            prompt=prompt,
            enable_mcp_discover=enable_mcp_discover,
            mcp_config=mcp_config,
            mcp_server_names=mcp_server_names,
        )
        outputs.append((item, text))
    return outputs


def main() -> None:
    args = build_parser().parse_args()
    backend = cast(BackendName, args.backend)
    profile = cast(ProfileName, args.profile)
    names = [
        value.strip()
        for value in str(args.mcp_server_names).split(",")
        if value.strip()
    ]
    results = asyncio.run(
        run_selected_profiles(
            backend=backend,
            profile=profile,
            prompt=str(args.prompt),
            enable_mcp_discover=bool(args.mcp_discover),
            mcp_config=str(args.mcp_config),
            mcp_server_names=names,
        )
    )
    for profile_name, text in results:
        print(f"\n=== APER Profile: {profile_name.upper()} ===\n")
        print(text)


if __name__ == "__main__":
    main()
