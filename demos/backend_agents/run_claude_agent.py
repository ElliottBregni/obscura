"""Run a backend-specific test agent on Claude."""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence

from demos.backend_agents.common import BackendAgentConfig, run_backend_agent

CLAUDE_CONFIG = BackendAgentConfig(
    name="claude-test-agent",
    backend_model="claude",
    role="agent:claude",
    system_prompt="You are a Claude backend parity test agent.",
    memory_namespace="demo:claude",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the Claude test agent demo")
    parser.add_argument(
        "--prompt",
        "-p",
        default="Summarize this repo in three bullets.",
        help="Prompt to run against the Claude backend.",
    )
    parser.add_argument(
        "--stream",
        action="store_true",
        help="Use streaming mode instead of single-shot run.",
    )
    parser.add_argument(
        "--start-timeout",
        type=float,
        default=20.0,
        help="Timeout (seconds) for runtime/backend startup.",
    )
    parser.add_argument(
        "--run-timeout",
        type=float,
        default=120.0,
        help="Timeout (seconds) for response generation.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        result = asyncio.run(
            run_backend_agent(
                CLAUDE_CONFIG,
                args.prompt,
                stream=args.stream,
                start_timeout_seconds=args.start_timeout,
                run_timeout_seconds=args.run_timeout,
            )
        )
    except TimeoutError as exc:
        print(f"Claude test agent timeout: {exc}", file=sys.stderr)
        print(
            "Check `claude auth status --json` and retry with a larger "
            "--run-timeout if needed.",
            file=sys.stderr,
        )
        raise SystemExit(2) from None
    print(result)


if __name__ == "__main__":
    main()
