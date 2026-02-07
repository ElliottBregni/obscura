"""
sdk.cli — CLI entry point for the unified SDK wrapper.

Usage::

    obscura-sdk copilot -p "explain this code"
    obscura-sdk claude -p "summarize this file" --model claude-sonnet-4-5-20250929
    cat file.py | obscura-sdk copilot --model-alias copilot_batch_diagrammer --automation-safe
    obscura-sdk claude --session abc123 -p "continue"
    obscura-sdk copilot --list-sessions
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any

from sdk._types import Backend, ChunkKind, SessionRef
from sdk.client import ObscuraClient


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="obscura-sdk",
        description="Unified CLI for Copilot and Claude SDK access.",
    )

    p.add_argument(
        "backend",
        choices=["copilot", "claude"],
        help="Which backend to use.",
    )
    p.add_argument(
        "-p", "--prompt",
        help="Prompt to send. Reads stdin if omitted.",
    )
    p.add_argument(
        "--model",
        default=None,
        help="Raw model ID (e.g. gpt-5-mini, claude-sonnet-4-5-20250929).",
    )
    p.add_argument(
        "--model-alias",
        default=None,
        help="copilot_models alias (e.g. copilot_automation_safe).",
    )
    p.add_argument(
        "--automation-safe",
        action="store_true",
        help="Require automation-safe model (copilot only).",
    )
    p.add_argument(
        "--system-prompt",
        default="",
        help="System prompt for the conversation.",
    )
    p.add_argument(
        "--stream",
        action="store_true",
        default=True,
        help="Stream output (default).",
    )
    p.add_argument(
        "--no-stream",
        dest="stream",
        action="store_false",
        help="Wait for full response.",
    )
    p.add_argument(
        "--session",
        default=None,
        help="Session ID to resume.",
    )
    p.add_argument(
        "--list-sessions",
        action="store_true",
        help="List available sessions and exit.",
    )

    # Claude-specific
    p.add_argument(
        "--permission-mode",
        default="default",
        choices=["default", "acceptEdits", "plan", "bypassPermissions"],
        help="Claude permission mode (claude only).",
    )
    p.add_argument(
        "--cwd",
        default=None,
        help="Working directory for Claude (claude only).",
    )

    return p


# ---------------------------------------------------------------------------
# Async runner
# ---------------------------------------------------------------------------

async def _run(args: argparse.Namespace) -> int:
    # Resolve prompt
    prompt = args.prompt
    if prompt is None and not args.list_sessions:
        if sys.stdin.isatty():
            print("Enter prompt (Ctrl+D to send):", file=sys.stderr)
        prompt = sys.stdin.read().strip()
        if not prompt:
            print("Error: empty prompt.", file=sys.stderr)
            return 1

    try:
        async with ObscuraClient(
            args.backend,
            model=args.model,
            model_alias=args.model_alias,
            automation_safe=args.automation_safe,
            system_prompt=args.system_prompt,
            permission_mode=args.permission_mode,
            cwd=args.cwd,
        ) as client:

            # List sessions mode
            if args.list_sessions:
                sessions = await client.list_sessions()
                if not sessions:
                    print("No sessions found.", file=sys.stderr)
                else:
                    for s in sessions:
                        print(f"  {s.session_id}  ({s.backend.value})")
                return 0

            # Resume session
            if args.session:
                ref = SessionRef(
                    session_id=args.session,
                    backend=Backend(args.backend),
                )
                await client.resume_session(ref)

            # Send prompt
            if args.stream:
                async for chunk in client.stream(prompt):
                    if chunk.kind == ChunkKind.TEXT_DELTA:
                        print(chunk.text, end="", flush=True)
                    elif chunk.kind == ChunkKind.THINKING_DELTA:
                        # Dim gray for thinking
                        print(f"\033[90m{chunk.text}\033[0m", end="", flush=True)
                    elif chunk.kind == ChunkKind.TOOL_USE_START:
                        print(f"\n\033[36m[tool: {chunk.tool_name}]\033[0m", file=sys.stderr)
                    elif chunk.kind == ChunkKind.ERROR:
                        print(f"\n\033[31m[error] {chunk.text}\033[0m", file=sys.stderr)
                print()  # trailing newline
            else:
                response = await client.send(prompt)
                print(response.text)

    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130

    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
