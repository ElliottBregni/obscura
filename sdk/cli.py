"""
sdk.cli -- CLI entry point for the unified SDK wrapper.

Usage::

    obscura-sdk copilot -p "explain this code"
    obscura-sdk claude -p "summarize this file" --model claude-sonnet-4-5-20250929
    cat file.py | obscura-sdk copilot --model-alias copilot_batch_diagrammer --automation-safe
    obscura-sdk claude --session abc123 -p "continue"
    obscura-sdk copilot --list-sessions
    obscura-sdk serve [--host 0.0.0.0] [--port 8080] [--reload]
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

def _add_agent_arguments(parser: argparse.ArgumentParser) -> None:
    """Add the common agent arguments to a subparser."""
    parser.add_argument(
        "-p", "--prompt",
        help="Prompt to send. Reads stdin if omitted.",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Raw model ID (e.g. gpt-5-mini, claude-sonnet-4-5-20250929).",
    )
    parser.add_argument(
        "--model-alias",
        default=None,
        help="copilot_models alias (e.g. copilot_automation_safe).",
    )
    parser.add_argument(
        "--automation-safe",
        action="store_true",
        help="Require automation-safe model (copilot only).",
    )
    parser.add_argument(
        "--system-prompt",
        default="",
        help="System prompt for the conversation.",
    )
    parser.add_argument(
        "--stream",
        action="store_true",
        default=True,
        help="Stream output (default).",
    )
    parser.add_argument(
        "--no-stream",
        dest="stream",
        action="store_false",
        help="Wait for full response.",
    )
    parser.add_argument(
        "--session",
        default=None,
        help="Session ID to resume.",
    )
    parser.add_argument(
        "--list-sessions",
        action="store_true",
        help="List available sessions and exit.",
    )

    # Claude-specific
    parser.add_argument(
        "--permission-mode",
        default="default",
        choices=["default", "acceptEdits", "plan", "bypassPermissions"],
        help="Claude permission mode (claude only).",
    )
    parser.add_argument(
        "--cwd",
        default=None,
        help="Working directory for Claude (claude only).",
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="obscura-sdk",
        description="Unified CLI for Copilot and Claude SDK access.",
    )

    sub = p.add_subparsers(dest="command", help="Available commands")

    # -- copilot / claude subcommands ------------------------------------
    copilot_parser = sub.add_parser("copilot", help="Use Copilot backend")
    _add_agent_arguments(copilot_parser)

    claude_parser = sub.add_parser("claude", help="Use Claude backend")
    _add_agent_arguments(claude_parser)

    # -- serve subcommand ------------------------------------------------
    serve_parser = sub.add_parser("serve", help="Start the HTTP API server")
    serve_parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Bind address (default: 0.0.0.0).",
    )
    serve_parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="Listen port (default: 8080).",
    )
    serve_parser.add_argument(
        "--reload",
        action="store_true",
        help="Enable auto-reload for development.",
    )
    serve_parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Number of worker processes (default: 1).",
    )

    return p


# ---------------------------------------------------------------------------
# Async runner (agent commands)
# ---------------------------------------------------------------------------

async def _run(args: argparse.Namespace) -> int:
    backend = args.command  # "copilot" or "claude"

    # Initialize telemetry if available (CLI mode — text logging, no auth user)
    _init_cli_telemetry()
    log = _get_cli_logger(__name__)

    # Resolve prompt
    prompt = args.prompt
    if prompt is None and not args.list_sessions:
        if sys.stdin.isatty():
            log.info("cli.prompt_wait", msg="Enter prompt (Ctrl+D to send)")
        prompt = sys.stdin.read().strip()
        if not prompt:
            log.error("cli.empty_prompt", msg="Error: empty prompt")
            return 1

    try:
        async with ObscuraClient(
            backend,
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
                    log.info("cli.no_sessions", msg="No sessions found")
                else:
                    for s in sessions:
                        print(f"  {s.session_id}  ({s.backend.value})")
                return 0

            # Resume session
            if args.session:
                ref = SessionRef(
                    session_id=args.session,
                    backend=Backend(backend),
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
                        log.info("cli.tool_use", tool_name=chunk.tool_name)
                    elif chunk.kind == ChunkKind.ERROR:
                        log.error("cli.stream_error", error=chunk.text)
                print()  # trailing newline
            else:
                response = await client.send(prompt)
                print(response.text)

    except ValueError as e:
        log.error("cli.error", error=str(e))
        return 1
    except KeyboardInterrupt:
        log.info("cli.interrupted", msg="Interrupted")
        return 130

    return 0


# ---------------------------------------------------------------------------
# Serve runner
# ---------------------------------------------------------------------------

def _run_serve(args: argparse.Namespace) -> int:
    """Start the uvicorn server with the FastAPI app."""
    try:
        import uvicorn
    except ImportError:
        print(
            "Error: uvicorn is not installed. "
            "Install server extras: pip install 'fv-copilot[server]'",
            file=sys.stderr,
        )
        return 1

    uvicorn.run(
        "sdk.server:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        reload=args.reload,
        workers=args.workers,
    )
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 1

    if args.command == "serve":
        return _run_serve(args)

    # copilot / claude
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())


# ---------------------------------------------------------------------------
# Telemetry helpers (no-op when dependencies are unavailable)
# ---------------------------------------------------------------------------

def _init_cli_telemetry() -> None:
    """Initialize telemetry for CLI mode with text logging."""
    try:
        from sdk.config import ObscuraConfig
        from sdk.telemetry import init_telemetry

        config = ObscuraConfig.from_env()
        # CLI always uses text format for human-readable output
        config.log_format = "text"
        init_telemetry(config)
    except Exception:
        pass


class _StderrLogger:
    """Minimal fallback logger that writes to stderr."""

    def info(self, event: str, **kw: Any) -> None:
        msg = kw.get("msg", event)
        print(msg, file=sys.stderr)

    def error(self, event: str, **kw: Any) -> None:
        msg = kw.get("error", kw.get("msg", event))
        print(f"Error: {msg}", file=sys.stderr)

    def warning(self, event: str, **kw: Any) -> None:
        msg = kw.get("msg", event)
        print(f"Warning: {msg}", file=sys.stderr)


def _get_cli_logger(name: str) -> Any:
    """Return a structlog logger, or a stderr fallback."""
    try:
        from sdk.telemetry.logging import get_logger
        return get_logger(name)
    except Exception:
        return _StderrLogger()
