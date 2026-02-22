"""
obscura.cli -- CLI entry point for the unified SDK wrapper.

Usage::

    obscura-sdk copilot -p "explain this code"
    obscura-sdk claude -p "summarize this file" --model claude-sonnet-4-5-20250929
    obscura-sdk openai -p "summarize this" --model gpt-4o
    obscura-sdk moonshot -p "summarize this" --model kimi-2.5
    obscura-sdk localllm -p "hello from localhost"
    cat file.py | obscura-sdk copilot --model-alias copilot_batch_diagrammer --automation-safe
    obscura-sdk claude --session abc123 -p "continue"
    obscura-sdk copilot --list-sessions
    obscura-sdk serve [--host 0.0.0.0] [--port 8080] [--reload]
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
from typing import Any, cast

from obscura.auth.models import AuthenticatedUser
from obscura.core.types import Backend, ChunkKind, SessionRef
from obscura.core.client import ObscuraClient
from obscura.memory import MemoryStore


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def _add_agent_arguments(parser: argparse.ArgumentParser) -> None:
    """Add the common agent arguments to a subparser."""
    parser.add_argument(
        "-p",
        "--prompt",
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
        "--mode",
        default="unified",
        choices=["unified", "native"],
        help="Execution mode: unified wrapper (default) or native SDK calls.",
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
        description="Unified CLI for Copilot, Claude, OpenAI, Moonshot, and LocalLLM backends.",
    )

    sub = p.add_subparsers(dest="command", help="Available commands")

    # -- backend subcommands ---------------------------------------------
    copilot_parser = sub.add_parser("copilot", help="Use Copilot backend")
    _add_agent_arguments(copilot_parser)

    claude_parser = sub.add_parser("claude", help="Use Claude backend")
    _add_agent_arguments(claude_parser)

    openai_parser = sub.add_parser(
        "openai", help="Use OpenAI backend (or compatible provider)"
    )
    _add_agent_arguments(openai_parser)

    moonshot_parser = sub.add_parser("moonshot", help="Use Moonshot/Kimi backend")
    _add_agent_arguments(moonshot_parser)

    localllm_parser = sub.add_parser(
        "localllm", help="Use local LLM backend (LM Studio, Ollama, etc.)"
    )
    _add_agent_arguments(localllm_parser)

    # -- passthrough subcommand ------------------------------------------
    passthrough_parser = sub.add_parser(
        "passthrough",
        help="Run a vendor CLI directly (native passthrough mode).",
    )
    passthrough_parser.add_argument(
        "vendor",
        choices=["copilot", "claude", "codex", "openai", "moonshot", "localllm"],
        help="Vendor CLI family to execute.",
    )
    passthrough_parser.add_argument(
        "--capture",
        action="store_true",
        help="Capture transcript output to ~/.obscura/transcripts.",
    )
    passthrough_parser.add_argument(
        "vendor_args",
        nargs=argparse.REMAINDER,
        help="Arguments passed to the vendor CLI (prefix with --).",
    )

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

    # -- tui subcommand --------------------------------------------------
    tui_parser = sub.add_parser("tui", help="Launch interactive TUI")
    tui_parser.add_argument(
        "--backend",
        default="copilot",
        choices=["copilot", "claude", "openai", "moonshot", "localllm"],
        help="Backend to use (default: copilot).",
    )
    tui_parser.add_argument(
        "--model",
        default=None,
        help="Model ID override.",
    )
    tui_parser.add_argument(
        "--cwd",
        default=".",
        help="Working directory for file operations.",
    )
    tui_parser.add_argument(
        "--session",
        default=None,
        help="Resume a saved TUI session by ID.",
    )
    tui_parser.add_argument(
        "--mode",
        default="ask",
        choices=["ask", "plan", "code", "diff"],
        help="Initial mode (default: ask).",
    )

    # -- observe subcommand ----------------------------------------------
    observe_parser = sub.add_parser(
        "observe",
        help="Tail agent runtime state from memory and highlight stalled agents.",
    )
    observe_parser.add_argument(
        "--user-id",
        required=True,
        help="User ID whose memory database should be observed.",
    )
    observe_parser.add_argument(
        "--email",
        default="observe@obscura.local",
        help="Email used to construct the observer user identity.",
    )
    observe_parser.add_argument(
        "--org-id",
        default="org-observe",
        help="Org ID used to construct the observer user identity.",
    )
    observe_parser.add_argument(
        "--namespace",
        default="agent:runtime",
        help="Memory namespace containing agent state records.",
    )
    observe_parser.add_argument(
        "--interval-seconds",
        type=float,
        default=1.0,
        help="Polling interval in seconds (default: 1.0).",
    )
    observe_parser.add_argument(
        "--stale-seconds",
        type=float,
        default=20.0,
        help="Emit stalled warning when RUNNING/WAITING state is older than this threshold.",
    )
    observe_parser.add_argument(
        "--duration-seconds",
        type=float,
        default=0.0,
        help="Optional max observe duration. 0 means run until interrupted.",
    )
    observe_parser.add_argument(
        "--once",
        action="store_true",
        help="Print one snapshot and exit.",
    )

    return p


# ---------------------------------------------------------------------------
# Async runner (agent commands)
# ---------------------------------------------------------------------------

_AGENT_COMMANDS: frozenset[str] = frozenset(
    {"copilot", "claude", "openai", "moonshot", "localllm"}
)


async def _run(args: argparse.Namespace) -> int:
    backend: str = (
        args.command
    )  # "copilot", "claude", "openai", "moonshot", or "localllm"

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

            if args.mode == "native":
                await _run_native(client, backend, prompt, args.stream, log)
                return 0

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


async def _run_native(
    client: ObscuraClient,
    backend: str,
    prompt: str,
    stream: bool,
    log: Any,
) -> None:
    """Execute one request using raw provider SDK objects."""
    handle = client.native
    backend_impl = client.backend_impl

    if backend in ("openai", "moonshot", "localllm"):
        raw = handle.client
        model = getattr(backend_impl, "model", None) or "default"
        if stream:
            response = await raw.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                stream=True,
            )
            async for chunk in response:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                if getattr(delta, "content", None):
                    print(delta.content, end="", flush=True)
            print()
        else:
            response = await raw.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
            )
            text = response.choices[0].message.content or ""
            print(text)
        return

    if backend == "copilot":
        session = handle.session
        response = await session.send_and_wait({"prompt": prompt})
        text = _extract_copilot_text(response)
        print(text)
        return

    if backend == "claude":
        raw = handle.client
        await raw.query(prompt)
        emitted = False
        async for msg in raw.receive_response():
            if type(msg).__name__ != "AssistantMessage":
                continue
            for block in getattr(msg, "content", []) or []:
                if type(block).__name__ == "TextBlock":
                    txt = getattr(block, "text", "")
                    if txt:
                        print(txt, end="", flush=True)
                        emitted = True
        if emitted:
            print()
        return

    log.error("cli.native_unsupported", error=f"Unsupported backend: {backend}")
    raise ValueError(f"Unsupported backend: {backend}")


def _extract_copilot_text(response: Any) -> str:
    """Extract text content from a Copilot SDK response object."""
    if hasattr(response, "data") and hasattr(response.data, "content"):
        return str(response.data.content or "")
    if hasattr(response, "content"):
        return str(response.content or "")
    if isinstance(response, str):
        return response
    return str(response)


def _resolve_passthrough_cmd(vendor: str) -> list[str]:
    """Resolve the executable command for passthrough vendor selection."""
    env_key = f"OBSCURA_PASSTHROUGH_{vendor.upper()}_CMD"
    configured = os.environ.get(env_key, "").strip()
    if configured:
        return shlex.split(configured)

    defaults: dict[str, list[str]] = {
        "copilot": ["copilot"],
        "claude": ["claude"],
        "codex": ["codex"],
        "openai": ["openai"],
        "moonshot": ["moonshot", "kimi"],
        "localllm": ["ollama"],
    }
    return defaults[vendor]


def _run_passthrough(args: argparse.Namespace) -> int:
    """Run a vendor CLI directly, optionally capturing output."""
    vendor_args = list(args.vendor_args or [])
    if vendor_args and vendor_args[0] == "--":
        vendor_args = vendor_args[1:]

    full_cmd = _resolve_passthrough_cmd(args.vendor) + vendor_args
    if not full_cmd:
        print("Error: no vendor command resolved", file=sys.stderr)
        return 1

    if args.capture:
        return asyncio.run(_run_passthrough_captured(args.vendor, full_cmd))

    proc = subprocess.run(full_cmd)
    return int(proc.returncode)


async def _run_passthrough_captured(vendor: str, full_cmd: list[str]) -> int:
    """Run passthrough command and capture transcript to local file."""
    proc = await asyncio.create_subprocess_exec(
        *full_cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    transcript: list[str] = []

    async def _stream(src: Any, is_err: bool = False) -> None:
        while True:
            line = await src.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace")
            transcript.append(text)
            target = sys.stderr if is_err else sys.stdout
            print(text, end="", file=target)

    await asyncio.gather(
        _stream(proc.stdout, is_err=False),
        _stream(proc.stderr, is_err=True),
    )
    await proc.wait()

    ts = int(time.time())
    session_id = f"passthrough_{vendor}_{ts}"
    transcript_dir = Path.home() / ".obscura" / "transcripts"
    transcript_dir.mkdir(parents=True, exist_ok=True)
    out_path = transcript_dir / f"{session_id}.txt"
    out_path.write_text("".join(transcript)[:50000], encoding="utf-8")
    print(f"\n[obscura-sdk] transcript saved: {out_path}", file=sys.stderr)
    return int(proc.returncode) if proc.returncode is not None else 1


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
            "Install server extras: pip install 'obscura[server]'",
            file=sys.stderr,
        )
        return 1

    uvicorn.run(
        "obscura.server:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        reload=args.reload,
        workers=args.workers,
    )
    return 0


# ---------------------------------------------------------------------------
# TUI runner
# ---------------------------------------------------------------------------


def _run_tui(args: argparse.Namespace) -> int:
    """Launch the interactive TUI."""
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    try:
        from obscura.tui.app import run_tui
    except ImportError:
        print(
            "Error: TUI dependencies not installed. "
            "Install with: pip install 'obscura[tui]'",
            file=sys.stderr,
        )
        return 1

    run_tui(
        backend=args.backend,
        model=args.model,
        cwd=args.cwd,
        session=args.session,
        mode=args.mode,
    )
    return 0


@dataclass(frozen=True)
class ObservedAgentState:
    """Compact runtime state object used by the observe command."""

    agent_id: str
    name: str
    status: str
    updated_at: datetime
    iteration_count: int
    error_message: str | None

    def signature(self) -> tuple[str, str, int, str | None]:
        return (
            self.status,
            self.updated_at.isoformat(),
            self.iteration_count,
            self.error_message,
        )

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> ObservedAgentState | None:
        agent_id = payload.get("agent_id")
        if not isinstance(agent_id, str) or not agent_id.strip():
            return None
        name = payload.get("name")
        status = payload.get("status")
        updated_raw = payload.get("updated_at")
        if not isinstance(name, str) or not isinstance(status, str):
            return None
        if not isinstance(updated_raw, str):
            return None
        updated_at = _parse_iso_datetime(updated_raw)
        if updated_at is None:
            return None
        iteration_raw = payload.get("iteration_count", 0)
        iteration_count = int(iteration_raw) if isinstance(iteration_raw, int) else 0
        error_message = payload.get("error_message")
        if error_message is not None and not isinstance(error_message, str):
            error_message = str(error_message)
        return cls(
            agent_id=agent_id,
            name=name,
            status=status,
            updated_at=updated_at,
            iteration_count=iteration_count,
            error_message=error_message,
        )


def _parse_iso_datetime(value: str) -> datetime | None:
    raw = value.strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = f"{raw[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _build_observe_user(args: argparse.Namespace) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=str(args.user_id),
        email=str(args.email),
        roles=("operator",),
        org_id=str(args.org_id),
        token_type="user",
        raw_token="observe-token",
    )


def collect_observed_agent_states(
    store: MemoryStore,
    *,
    namespace: str,
) -> list[ObservedAgentState]:
    states: list[ObservedAgentState] = []
    for key in store.list_keys(namespace=namespace):
        if not key.key.startswith("agent_state_"):
            continue
        payload = store.get(key.key, namespace=namespace)
        if not isinstance(payload, dict):
            continue
        payload_dict = cast(dict[Any, Any], payload)
        typed_payload: dict[str, Any] = {}
        for raw_key, raw_value in payload_dict.items():
            key_name = raw_key if isinstance(raw_key, str) else str(raw_key)
            typed_payload[key_name] = raw_value
        state = ObservedAgentState.from_payload(typed_payload)
        if state is None:
            continue
        states.append(state)
    return sorted(states, key=lambda entry: (entry.updated_at, entry.agent_id))


def find_stale_agent_ids(
    states: list[ObservedAgentState],
    *,
    now: datetime,
    stale_seconds: float,
) -> list[str]:
    stale: list[str] = []
    for state in states:
        if state.status not in {"RUNNING", "WAITING"}:
            continue
        age = (now - state.updated_at).total_seconds()
        if age >= stale_seconds:
            stale.append(state.agent_id)
    return stale


def _render_state_line(state: ObservedAgentState) -> str:
    updated = state.updated_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%SZ")
    base = (
        f"{state.agent_id} name={state.name} status={state.status} "
        f"iter={state.iteration_count} updated={updated}"
    )
    if state.error_message:
        return f"{base} error={state.error_message}"
    return base


def run_observe(args: argparse.Namespace) -> int:
    interval = max(0.1, float(args.interval_seconds))
    stale_seconds = max(1.0, float(args.stale_seconds))
    duration_seconds = max(0.0, float(args.duration_seconds))
    namespace = str(args.namespace)

    user = _build_observe_user(args)
    store = MemoryStore.for_user(user)
    stats = store.get_stats()
    db_path = str(stats.get("db_path", "unknown"))
    print(
        f"[observe] user={user.user_id} namespace={namespace} db={db_path} "
        f"interval={interval:.1f}s stale={stale_seconds:.1f}s",
        flush=True,
    )

    previous_by_id: dict[str, tuple[str, str, int, str | None]] = {}
    stale_alerts: set[tuple[str, str]] = set()
    started = time.monotonic()

    try:
        while True:
            now = datetime.now(UTC)
            states = collect_observed_agent_states(store, namespace=namespace)
            current_ids = {state.agent_id for state in states}

            for state in states:
                signature = state.signature()
                if previous_by_id.get(state.agent_id) != signature:
                    print(_render_state_line(state), flush=True)

            stale_ids = set(
                find_stale_agent_ids(states, now=now, stale_seconds=stale_seconds)
            )
            for state in states:
                if state.agent_id not in stale_ids:
                    continue
                signature_key = (state.agent_id, state.updated_at.isoformat())
                if signature_key in stale_alerts:
                    continue
                age = (now - state.updated_at).total_seconds()
                print(
                    f"WARNING: stalled agent {state.agent_id} "
                    f"(status={state.status}, age={age:.1f}s)",
                    flush=True,
                )
                stale_alerts.add(signature_key)

            removed_ids = set(previous_by_id) - current_ids
            for agent_id in sorted(removed_ids):
                print(f"{agent_id} removed from namespace={namespace}", flush=True)

            previous_by_id = {state.agent_id: state.signature() for state in states}

            if bool(args.once):
                break
            if (
                duration_seconds > 0
                and (time.monotonic() - started) >= duration_seconds
            ):
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        return 130

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

    if args.command == "tui":
        return _run_tui(args)

    if args.command == "passthrough":
        return _run_passthrough(args)

    if args.command == "observe":
        return run_observe(args)

    if args.command in _AGENT_COMMANDS:
        return asyncio.run(_run(args))

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())


# ---------------------------------------------------------------------------
# Telemetry helpers (no-op when dependencies are unavailable)
# ---------------------------------------------------------------------------


def _init_cli_telemetry() -> None:
    """Initialize telemetry for CLI mode with text logging."""
    try:
        from obscura.core.config import ObscuraConfig
        from obscura.telemetry import init_telemetry

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
        from obscura.telemetry.logging import get_logger

        return get_logger(name)
    except Exception:
        return _StderrLogger()
