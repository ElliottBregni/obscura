"""Run a backend-specific test agent on Copilot."""

from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys
from collections.abc import Sequence

from demos.backend_agents.common import BackendAgentConfig, run_backend_agent

COPILOT_CONFIG = BackendAgentConfig(
    name="copilot-test-agent",
    backend_model="copilot",
    role="agent:copilot",
    system_prompt="You are a Copilot backend parity test agent.",
    memory_namespace="demo:copilot",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the Copilot test agent demo")
    parser.add_argument(
        "--prompt",
        "-p",
        default="Summarize this repository in three bullets.",
        help="Prompt to run against the Copilot backend.",
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
    parser.add_argument(
        "--sdk-first",
        action="store_true",
        help="Try Obscura Copilot SDK lane first; default is OAuth-first via `copilot` CLI.",
    )
    parser.add_argument(
        "--no-cli-fallback",
        action="store_true",
        help="Disable fallback to `copilot` CLI if SDK lane fails.",
    )
    return parser


def run_copilot_cli_oauth(prompt: str, *, timeout_seconds: float) -> str:
    """Run prompt through Copilot CLI OAuth lane and return output text."""
    cmd = ["copilot", "-p", prompt]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
        timeout=max(1, int(timeout_seconds)),
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout).strip()
        raise RuntimeError(f"copilot CLI failed: {err}")
    text = (proc.stdout or "").strip()
    if not text:
        raise RuntimeError("copilot CLI returned empty output.")
    return text


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if not args.sdk_first:
        try:
            result = run_copilot_cli_oauth(
                args.prompt, timeout_seconds=args.run_timeout
            )
            print(result)
            return
        except Exception:
            if args.no_cli_fallback:
                print(
                    "Copilot CLI OAuth lane failed and --no-cli-fallback is set.",
                    file=sys.stderr,
                )
                raise SystemExit(3) from None

    try:
        result = asyncio.run(
            run_backend_agent(
                COPILOT_CONFIG,
                args.prompt,
                stream=args.stream,
                start_timeout_seconds=args.start_timeout,
                run_timeout_seconds=args.run_timeout,
            )
        )
    except TimeoutError as exc:
        print(f"Copilot test agent timeout: {exc}", file=sys.stderr)
        print(
            "Check GitHub auth (`gh auth status`) and retry with a larger "
            "--run-timeout if needed.",
            file=sys.stderr,
        )
        raise SystemExit(2) from None
    except Exception as exc:
        if not args.no_cli_fallback:
            try:
                result = run_copilot_cli_oauth(
                    args.prompt,
                    timeout_seconds=args.run_timeout,
                )
            except Exception as fallback_exc:
                print(
                    "Copilot SDK lane failed and CLI OAuth fallback failed: "
                    f"{fallback_exc}",
                    file=sys.stderr,
                )
                raise SystemExit(1) from None
        else:
            print(f"Copilot test agent failed: {exc}", file=sys.stderr)
            print(
                "If this is an auth issue, run `gh auth login` or set GH_TOKEN.",
                file=sys.stderr,
            )
            raise SystemExit(1) from None
    print(result)


if __name__ == "__main__":
    main()
