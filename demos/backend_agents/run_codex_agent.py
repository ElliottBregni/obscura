"""Run a backend-specific test agent on Codex/OpenAI."""

from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
import tempfile
from collections.abc import Sequence

from demos.backend_agents.common import BackendAgentConfig, run_backend_agent

CODEX_CONFIG = BackendAgentConfig(
    name="codex-test-agent",
    backend_model="openai",
    role="agent:openai",
    system_prompt="You are a Codex/OpenAI backend parity test agent.",
    memory_namespace="demo:codex",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the Codex/OpenAI test agent demo")
    parser.add_argument(
        "--prompt",
        "-p",
        default="Write a tiny Python function with tests.",
        help="Prompt to run against the Codex/OpenAI backend.",
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
        "--no-cli-fallback",
        action="store_true",
        help="Disable fallback to `codex exec` when OpenAI billing is inactive.",
    )
    parser.add_argument(
        "--sdk-first",
        action="store_true",
        help="Try Obscura OpenAI SDK lane first; default is OAuth-first via `codex exec`.",
    )
    return parser


def run_codex_cli_oauth(prompt: str, *, timeout_seconds: float) -> str:
    """Run prompt through Codex CLI OAuth lane and return final message."""
    status = subprocess.run(
        ["codex", "login", "status"],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    status_text = f"{status.stdout}\n{status.stderr}"
    if status.returncode != 0 or "Logged in" not in status_text:
        raise RuntimeError("Codex CLI is not logged in. Run `codex login` first.")

    tmp = tempfile.NamedTemporaryFile(prefix="codex-last-", suffix=".txt", delete=False)
    tmp.close()
    path = tmp.name
    try:
        proc = subprocess.run(
            [
                "codex",
                "exec",
                "--skip-git-repo-check",
                "--output-last-message",
                path,
                prompt,
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=max(1, int(timeout_seconds)),
        )
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout).strip()
            raise RuntimeError(f"codex exec failed: {err}")

        final = ""
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                final = f.read().strip()
        if final:
            return final
        # Fallback if output-last-message file is empty for any reason.
        return (proc.stdout or "").strip()
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if not args.sdk_first:
        try:
            result = run_codex_cli_oauth(
                args.prompt,
                timeout_seconds=args.run_timeout,
            )
            print(result)
            return
        except Exception:
            # Fall back to SDK lane when OAuth CLI lane fails.
            pass

    try:
        result = asyncio.run(
            run_backend_agent(
                CODEX_CONFIG,
                args.prompt,
                stream=args.stream,
                start_timeout_seconds=args.start_timeout,
                run_timeout_seconds=args.run_timeout,
            )
        )
    except TimeoutError as exc:
        print(f"Codex/OpenAI test agent timeout: {exc}", file=sys.stderr)
        print(
            "Check your OpenAI/Codex auth env vars and retry with a larger "
            "--run-timeout if needed.",
            file=sys.stderr,
        )
        raise SystemExit(2) from None
    except Exception as exc:
        if not args.no_cli_fallback and "billing_not_active" in str(exc):
            try:
                result = run_codex_cli_oauth(
                    args.prompt,
                    timeout_seconds=args.run_timeout,
                )
            except Exception as fallback_exc:
                print(
                    "Codex/OpenAI backend failed with billing_not_active and "
                    f"CLI OAuth fallback failed: {fallback_exc}",
                    file=sys.stderr,
                )
                raise SystemExit(3) from None
        else:
            print(f"Codex/OpenAI test agent failed: {exc}", file=sys.stderr)
            raise SystemExit(1) from None
    print(result)


if __name__ == "__main__":
    main()
