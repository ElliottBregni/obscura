"""Run an agent with Playwright MCP tools enabled."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import cast

from obscura.agent.agents import AgentRuntime, MCPConfig
from obscura.demo.framework import DemoAgentConfig, run_demo_prompt


@dataclass(frozen=True)
class PlaywrightMCPDemoConfig:
    """Configuration for running the Playwright MCP demo."""

    model: str
    prompt: str
    stream: bool
    start_timeout_seconds: float
    run_timeout_seconds: float
    mcp_command: str
    mcp_args: tuple[str, ...]
    mcp_env: dict[str, str]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Playwright MCP tools demo")
    parser.add_argument(
        "--model",
        default="claude",
        help="Backend model to use (claude recommended for MCP tools).",
    )
    parser.add_argument(
        "--prompt",
        "-p",
        default=(
            "Use Playwright MCP tools to open https://example.com, "
            "then return the page title and first heading."
        ),
        help="Prompt to execute with Playwright MCP tools enabled.",
    )
    parser.add_argument(
        "--stream",
        action="store_true",
        help="Use streaming output.",
    )
    parser.add_argument(
        "--start-timeout",
        type=float,
        default=30.0,
        help="Timeout in seconds for runtime and agent startup.",
    )
    parser.add_argument(
        "--run-timeout",
        type=float,
        default=180.0,
        help="Timeout in seconds for prompt execution.",
    )
    parser.add_argument(
        "--mcp-command",
        default="npx",
        help="Command for Playwright MCP stdio server.",
    )
    parser.add_argument(
        "--mcp-args",
        nargs="*",
        default=["-y", "@playwright/mcp@latest"],
        help="Args for --mcp-command.",
    )
    parser.add_argument(
        "--mcp-env",
        default="{}",
        help='JSON object of env vars for MCP server, e.g. \'{"DEBUG":"pw:mcp"}\'.',
    )
    return parser


def parse_env_json(raw: str) -> dict[str, str]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid --mcp-env JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("--mcp-env must be a JSON object")
    result: dict[str, str] = {}
    payload_map = cast(dict[str, object], payload)
    for key, value in payload_map.items():
        result[str(key)] = str(value)
    return result


async def run_playwright_mcp_demo(config: PlaywrightMCPDemoConfig) -> str:
    mcp_server = {
        "transport": "stdio",
        "command": config.mcp_command,
        "args": list(config.mcp_args),
        "env": config.mcp_env,
    }
    demo_config = DemoAgentConfig(
        name="playwright-mcp-demo",
        model=config.model,
        role=f"agent:{config.model}",
        system_prompt=(
            "You are an MCP tools demo agent. Prefer tool use for browser tasks "
            "and report concise, factual results."
        ),
        memory_namespace="demo:playwright:mcp",
    )
    return await run_demo_prompt(
        demo_config,
        config.prompt,
        stream=config.stream,
        runtime_cls=AgentRuntime,
        start_timeout_seconds=config.start_timeout_seconds,
        run_timeout_seconds=config.run_timeout_seconds,
        spawn_kwargs={"mcp": MCPConfig(enabled=True, servers=[mcp_server])},
    )


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        env_map = parse_env_json(args.mcp_env)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from None

    config = PlaywrightMCPDemoConfig(
        model=args.model,
        prompt=args.prompt,
        stream=args.stream,
        start_timeout_seconds=args.start_timeout,
        run_timeout_seconds=args.run_timeout,
        mcp_command=args.mcp_command,
        mcp_args=tuple(cast(list[str], args.mcp_args)),
        mcp_env=env_map,
    )

    try:
        result = asyncio.run(run_playwright_mcp_demo(config))
    except TimeoutError as exc:
        print(f"Playwright MCP demo timed out: {exc}", file=sys.stderr)
        raise SystemExit(3) from None
    except Exception as exc:
        print(f"Playwright MCP demo failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from None

    print(result)


if __name__ == "__main__":
    main()
