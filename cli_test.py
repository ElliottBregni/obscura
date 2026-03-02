"""obscura.cli â Claude Code-style REPL for Obscura.

Single entry point: ``obscura`` drops into an interactive REPL.
Slash commands (``/help``, ``/agent``, ``/session``, etc.) for actions;
everything else is a chat message sent to the backend.

Usage::

    # Interactive REPL (default)
    obscura
    obscura -b claude
    obscura -b codex

    # Single-shot
    obscura "explain this code"
    obscura -b claude -m claude-sonnet-4-5-20250929 "summarize"
"""

from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path
from typing import Any

import click

from obscura import config
from obscura.cli.commands import (
    COMPLETIONS,
    REPLContext,
    _FILE_WRITE_TOOLS,
    handle_command,
    set_secret_menu_visibility,
)
from obscura.cli.prompt import (
    bordered_prompt,
    confirm_prompt_async,
    create_prompt_session,
)
from obscura.cli.render import (
    StreamRenderer,
    console,
    export_transcript_markdown,
    print_banner,
    print_user_turn,
    render_plan,
)
from obscura.cli import trace as trace_mod
from obscura.cli.vector_memory_bridge import (
    auto_save_turn,
    init_vector_store,
    load_startup_memories,
    search_relevant_context,
)
from obscura.core.client import ObscuraClient
from obscura.core.event_store import SQLiteEventStore, SessionStatus
from obscura.core.paths import resolve_obscura_home
from obscura.core.types import AgentEventKind, Backend, SessionRef, ToolChoice

_CONTEXT_WARNING_BANDS: tuple[int, ...] = (25, 50, 75)


# ---------------------------------------------------------------------------
# Session artifact helpers
# ---------------------------------------------------------------------------


def _session_dir(session_id: str) -> Path:
    return resolve_obscura_home() / "sessions" / session_id


def _write_session_transcript(session_id: str, history: list[tuple[str, str]]) -> None:
    try:
        session_dir = _session_dir(session_id)
        session_dir.mkdir(parents=True, exist_ok=True)
        transcript_path = session_dir / "transcript"
        transcript_path.write_text(export_transcript_markdown(history), encoding="utf-8")
    except Exception:
        pass


def _copilot_budget_pct(tokens: int, context_window: int) -> int:
    """Percent of Copilot's default soft context budget consumed."""
    budget_tokens = max(int(context_window * 0.50), 1)
    return int((tokens / budget_tokens) * 100)


def _emit_context_warnings(ctx: REPLContext, tokens: int, context_window: int) -> None:
    """Emit one-time warnings as context usage crosses 25/50/75%."""
    if context_window <= 0:
        return

    pct = int((tokens / context_window) * 100)

    # Allow warnings to retrigger after compaction if usage drops back below a band.
    ctx.context_warning_bands_seen = {
        band for band in ctx.context_warning_bands_seen if pct >= band
    }

    crossed = [
        band
        for band in _CONTEXT_WARNING_BANDS
        if pct >= band and band not in ctx.context_warning_bands_seen
    ]
    if not crossed:
        return

    for band in crossed:
        color = "dim yellow" if band < 75 else "yellow"
        msg = (
            f"[{color}]  Context warning: ~{tokens:,} tokens "
            f"({pct}% of {context_window:,}) crossed {band}%.[/]"
        )
        if ctx.backend == "copilot":
            budget_pct = _copilot_budget_pct(tokens, context_window)
            budget_tokens = int(context_window * 0.50)
            msg = (
                f"{msg}\n"
                f"[{color}]  Copilot budget: {budget_pct}% of "
                f"{budget_tokens:,} token soft budget.[/]"
            )
        console.print(msg)
        ctx.context_warning_bands_seen.add(band)


# ---------------------------------------------------------------------------
# MCP discovery
# ---------------------------------------------------------------------------


def _discover_mcp() -> tuple[list[dict[str, Any]], list[str]]:
    """Auto-discover MCP servers from ~/.obscura/mcp/. Returns (configs, names)."""
    try:
        from obscura.integrations.mcp.config_loader import (
            build_runtime_server_configs,
            discover_mcp_servers,
        )

        discovered = discover_mcp_servers()
        if discovered:
            configs = build_runtime_server_configs(discovered)
            names = [s.name for s in discovered]
            return configs, names
    except Exception:
        pass
    return [], []


# ---------------------------------------------------------------------------
# Tool confirmation callback
# ---------------------------------------------------------------------------


async def _cli_confirm(ctx: REPLContext, tool_name: str, tool_input: dict[str, Any]) -> bool:
    """Prompt user to approve a tool call. Returns True to allow."""
    if tool_name in ctx.confirm_allow:
        return True
    if tool_name in ctx.confirm_deny:
        console.print(f"\n[red]Denied by policy:[/] [bold]{tool_name}[/]")
        return False
    if ctx.confirm_default == "approve":
        return True
    if ctx.confirm_default == "deny":
        console.print(f"\n[red]Denied by default policy:[/] [bold]{tool_name}[/]")
        return False

    console.print(f"\n[yellow]Tool:[/] [bold]{tool_name}[/]")
    for k, v in tool_input.items():
        sv = str(v)
        if len(sv) > 80:
            sv = sv[:77] + "..."
        console.print(f"  [dim]{k}=[/]{sv}")

    for attempt in range(2):
        answer = await confirm_prompt_async(
            "Approve tool call? [a]pprove/[d]eny/[A]lways/[N]ever (default: deny) "
        )
        decision = _parse_confirm_decision(answer)
        if decision == "always":
            ctx.confirm_allow.add(tool_name)
            ctx.confirm_deny.discard(tool_name)
            return True
        if decision == "never":
            ctx.confirm_deny.add(tool_name)
            ctx.confirm_allow.discard(tool_name)
            return False
        if decision == "approve":
            return True
        if decision == "deny":
            return False
        if attempt == 0:
            console.print(
                "[red]Invalid response.[/] Enter approve/deny/always/never."
            )

    # Fail closed after one retry.
    console.print("[red]Invalid approval response; denying tool call.[/]")
    return False


def _parse_confirm_decision(answer: str) -> str | None:
    """Normalize confirmation input to one of: approve|deny|always|never."""
    val = answer.strip().lower()
    compact = " ".join(val.split())
    first = compact.split(" ", 1)[0] if compact else ""

    if compact in {"always"} or first in {"always"}:
        return "always"
    if compact in {"never"} or first in {"never"}:
        return "never"
    if compact in {"approve", "a", "y", "yes"} or first in {"approve", "a", "y", "yes"}:
        return "approve"
    if compact in {"deny", "d", "n", "no", ""} or first in {"deny", "d", "n", "no"}:
        return "deny"
    # Accept common longer confirmations from model/tool text.
    if compact.startswith("yes"):
        return "approve"
    if compact.startswith("no"):
        return "deny"
    return None


def _summarize_command(tool_input: dict[str, Any]) -> str:
    """Best-effort command preview for task surface."""
    for key in ("cmd", "command", "input", "script"):
        raw = tool_input.get(key)
        if raw:
            text = str(raw).replace("\n", " ").strip()
            return text[:120] + ("..." if len(text) > 120 else "")
    return str(tool_input)[:120]


def _is_python_execution(tool_name: str, tool_input: dict[str, Any]) -> bool:
    """Heuristic: identify python executions from tool calls."""
    if "python" in tool_name.lower():
        return True
    payload = " ".join(str(v).lower() for v in tool_input.values())
    hints = ("python ", "python3", "uv run python", "pytest", "ipython")
    return any(h in payload for h in hints)


def _track_task_surface_event(ctx: REPLContext, ev: Any) -> None:
    """Track python tool executions for /tasks."""
    if ev.kind == AgentEventKind.TOOL_CALL:
        if not _is_python_execution(ev.tool_name, ev.tool_input):
            return
        task_id = f"py-{len(ctx.python_tasks) + len(ctx._pending_python_tasks) + 1}"
        ctx._pending_python_tasks[ev.tool_use_id] = {
            "id": task_id,
            "status": "running",
            "tool": ev.tool_name,
            "command": _summarize_command(ev.tool_input),
        }
        return

    if ev.kind == AgentEventKind.TOOL_RESULT:
        pending = ctx._pending_python_tasks.pop(ev.tool_use_id, None)
        if pending is None:
            return
        pending["status"] = "error" if getattr(ev, "is_error", False) else "done"
        ctx.python_tasks.append(pending)
        if len(ctx.python_tasks) > 50:
            ctx.python_tasks = ctx.python_tasks[-50:]


# ---------------------------------------------------------------------------
# File change tracking
# ---------------------------------------------------------------------------


def _track_file_event(event: AgentEventKind, ctx: REPLContext, ev: Any) -> None:
    """Track file modifications for /diff."""
    if ev.kind == AgentEventKind.TOOL_CALL and ev.tool_name in _FILE_WRITE_TOOLS:
        path = ev.tool_input.get("path") or ev.tool_input.get("file_path", "")
        if path:
            try:
                before = Path(path).read_text()
            except (FileNotFoundError, OSError):
                before = ""
            ctx._pending_file_reads[ev.tool_use_id] = (path, before)

    elif ev.kind == AgentEventKind.TOOL_RESULT and ev.tool_use_id in ctx._pending_file_reads:
        path, before = ctx._pending_file_reads.pop(ev.tool_use_id)
        try:
            after = Path(path).read_text()
        except (FileNotFoundError, OSError):
            after = ""
        if after != before:
            ctx.add_file_change(path, before, after)


# ---------------------------------------------------------------------------
# Plan parsing
# ---------------------------------------------------------------------------


def _maybe_parse_plan(response_text: str, ctx: REPLContext) -> None:
    """If in PLAN mode, attempt to parse a structured plan from the response."""
    mm = ctx._mode_manager
    if mm is None:
        return

    from obscura.tui.modes import TUIMode

    if mm.current != TUIMode.PLAN:
        return

    if not response_text.strip():
        return

    from obscura.tui.modes import Plan

    plan = Plan.parse(response_text)
    if plan.steps:
        mm.active_plan = plan
        render_plan(plan)


# ---------------------------------------------------------------------------
# Chat message dispatch
# ---------------------------------------------------------------------------


MARKER_LINE
async def _cli_heartbeat(
    start_time: float,
    got_output: asyncio.Event,
    *,
    first_ping: float = 120.0,
    interval: float = 30.0,
) -> None:
    """Print keepalive dots if the agent takes longer than `first_ping` seconds
    to produce any output. After the first ping, repeats every `interval` s."""
    try:
        await asyncio.wait_for(got_output.wait(), timeout=first_ping)
    except asyncio.TimeoutError:
        pass
    else:
        return  # output arrived â nothing to do

    # First ping
    elapsed = time.time() - start_time
    console.print(
        f"[dim]  â£¿ still workingâ¦ ({elapsed:.0f}s)[/]",
        highlight=False,
    )

    # Repeat every `interval` seconds until output arrives
    while True:
        try:
            await asyncio.wait_for(got_output.wait(), timeout=interval)
        except asyncio.TimeoutError:
            elapsed = time.time() - start_time
            console.print(
                f"[dim]  â£¿ still workingâ¦ ({elapsed:.0f}s)[/]",
                highlight=False,
            )
        else:
            return


async def _cli_heartbeat(
    start_time: float,
    got_output: asyncio.Event,
    *,
    first_ping: float = 120.0,
    interval: float = 30.0,
) -> None:
    """Print keepalive dots if the agent takes longer than `first_ping` seconds
    to produce any output. After the first ping, repeats every `interval` s."""
    try:
        await asyncio.wait_for(got_output.wait(), timeout=first_ping)
    except asyncio.TimeoutError:
        pass
    else:
        return  # output arrived — nothing to do

    # First ping
    elapsed = time.time() - start_time
    console.print(
        f"[dim]  ⧿ still working… ({elapsed:.0f}s)[/]",
        highlight=False,
    )

    # Repeat every `interval` seconds until output arrives
    while True:
        try:
            await asyncio.wait_for(got_output.wait(), timeout=interval)
        except asyncio.TimeoutError:
            elapsed = time.time() - start_time
            console.print(
                f"[dim]  ⧿ still working… ({elapsed:.0f}s)[/]",
                highlight=False,
            )
        else:
            return


async def send_message(
    ctx: REPLContext,
    text: str,
    loop_kwargs: dict[str, Any],
    external_status: object | None = None,
    spinner_enabled: bool = True,
) -> str:
    """Send a chat message and stream the response with Markdown rendering.

    Returns the accumulated assistant text.
    """
    print_user_turn(text)
    renderer = StreamRenderer(
        external_status=external_status,
        spinner_enabled=spinner_enabled,
        model_name=ctx.model,
    )
    # Register active renderer so prompt can expand previews while streaming
    try:
        from obscura.cli.render import set_active_renderer

        set_active_renderer(renderer)
    except Exception:
        pass
    accumulated: list[str] = []

    # Build confirm callback if enabled
    confirm_cb = None
    if ctx.confirm_enabled:
        from obscura.core.types import ToolCallInfo

        async def confirm_cb(tc: ToolCallInfo) -> bool:
            return await _cli_confirm(ctx, tc.name, tc.input)

    # ââ Token-aware auto-compact ââââââââââââââââââââââââââââââââââââââââââââ
    # Use provider-specific thresholds from ctx.client so Claude (200k),
    # OpenAI (128k/16k), Copilot (128k), and Codex (128k) all get the
    # right limits without hard-coding numbers here.
    from obscura.cli.commands import _estimate_tokens, cmd_compact

    _context_window = ctx.client.context_window
    _compact_threshold = int(_context_window * 0.60)  # compact at 60%
    _warn_threshold = ctx.client.context_warn_threshold  # 50% of window

    # Update the system tool's token tracker so the LLM can introspect
    from obscura.tools.system import update_token_usage

    _pre_tokens = _estimate_tokens(
        "".join(t for _, t in ctx.message_history) + text
    )
    update_token_usage(
        input_tokens=_pre_tokens,
        context_window=_context_window,
        compact_threshold=_compact_threshold,
    )
    _emit_context_warnings(ctx, _pre_tokens, _context_window)

    if _pre_tokens > _compact_threshold:
        console.print(
            f"[yellow]â¡ Auto-compacting context (~{_pre_tokens:,} tokens, "
            f"60% of {_context_window:,}) â¦[/]"
        )
        await cmd_compact("6", ctx)

    # ââ Vector memory pre-search ââââââââââââââââââââââââââââââââââââââââââ
    augmented_text = text
    if ctx.vector_store is not None:
        vm_context = search_relevant_context(ctx.vector_store, text, top_k=3)
        if vm_context:
            augmented_text = f"{vm_context}\n\n---\n\n{text}"

    # ââ Streaming with graceful retry on context-limit errors ââââââââââââââââ
    _hb_start = time.time()
    _got_output: asyncio.Event = asyncio.Event()
    _hb_task: asyncio.Task[None] = asyncio.create_task(
        _cli_heartbeat(_hb_start, _got_output)
    )

    async def _stream_with_retry(attempt: int = 0) -> list[str]:
        _buf: list[str] = []
        _s = ctx.client.run_loop(
            augmented_text,
            max_turns=ctx.max_turns,
            event_store=ctx.store,
            session_id=ctx.session_id,
            auto_complete=False,
            on_confirm=confirm_cb,
            **loop_kwargs,
        )
        try:
            async for event in _s:
                renderer.handle(event)
                # Resolve confirmation gates emitted by the agent loop
                if (
                    event.kind == AgentEventKind.CONFIRMATION_REQUEST
                    and event.approval_future is not None
                    and not event.approval_future.done()
                ):
                    try:
                        answer = await confirm_prompt_async(
                            "  Approve tool call? [y/n] "
                        )
                        approved = answer.strip().lower() in (
                            "y", "yes", "a", "approve",
                        )
                        event.approval_future.set_result(approved)
                    except Exception:
                        event.approval_future.set_result(False)
                try:
                    preview = ""
                    if getattr(event, "text", None):
                        preview = event.text[:200]
                    elif getattr(event, "tool_result", None):
                        preview = str(event.tool_result)[:200]
                    elif getattr(event, "tool_input", None):
                        preview = str(event.tool_input)[:200]
                    tool_names = (
                        [event.tool_name]
                        if getattr(event, "tool_name", None)
                        else []
                    )
                    trace_mod.append_event(
                        event.kind.name,
                        preview=preview,
                        tool_names=tool_names,
                    )
                except Exception:
                    pass
                if event.kind == AgentEventKind.TEXT_DELTA:
                    _got_output.set()
                    _buf.append(event.text)
                _track_file_event(event.kind, ctx, event)
                _track_task_surface_event(ctx, event)
        except KeyboardInterrupt:
            pass
        except Exception as exc:
            _err = str(exc).lower()
            _is_ctx_err = any(
                kw in _err
                for kw in (
                    "prompt is too long",
                    "context window",
                    "too many tokens",
                    "maximum context length",
                    "request too large",
                )
            )
            if _is_ctx_err and attempt == 0:
                console.print(
                    "[red]â  Context limit reached â aggressive compact and retryâ¦[/]"
                )
                await cmd_compact("2", ctx)
                return await _stream_with_retry(attempt=1)
            raise
        return _buf

    try:
        accumulated = await _stream_with_retry()
    except KeyboardInterrupt:
        pass
    finally:
        _got_output.set()  # unblock heartbeat so it exits cleanly
        _hb_task.cancel()
        try:
            await _hb_task
        except asyncio.CancelledError:
            pass
        renderer.finish()
        try:
            from obscura.cli.render import set_active_renderer

            set_active_renderer(None)
        except Exception:
            pass

    console.print()  # newline after streaming

    response_text = "".join(accumulated)

    # Track message history
    ctx.message_history.append(("user", text))
    if response_text:
        ctx.message_history.append(("assistant", response_text))

    # ââ Vector memory auto-save âââââââââââââââââââââââââââââââââââââââââââ
    if ctx.vector_store is not None and response_text:
        turn_num = len([m for m in ctx.message_history if m[0] == "user"])
        auto_save_turn(
            ctx.vector_store,
            ctx.session_id,
            text,
            response_text,
            turn_number=turn_num,
        )

    # Post-send: update token tracker and show nudge
    _post_tokens = _estimate_tokens("".join(t for _, t in ctx.message_history))
    update_token_usage(
        input_tokens=_post_tokens,
        output_tokens=len(response_text) // 4,
        context_window=_context_window,
        compact_threshold=_compact_threshold,
    )
    if _warn_threshold < _post_tokens <= _compact_threshold:
        console.print(
            f"[dim yellow]  Context: ~{_post_tokens:,} tokens "
            f"({int(_post_tokens / _context_window * 100)}% of "
            f"{_context_window:,}). "
            f"Auto-compact at {_compact_threshold:,} (60%).[/]"
        )
    _emit_context_warnings(ctx, _post_tokens, _context_window)

    # Parse plan if in PLAN mode
    _maybe_parse_plan(response_text, ctx)
    _write_session_transcript(ctx.session_id, ctx.message_history)

    return response_text


# ---------------------------------------------------------------------------
# Async REPL
# ---------------------------------------------------------------------------


async def _repl(
    backend: str,
    model: str | None,
    system: str,
    session_id: str | None,
    max_turns: int,
    tools: str,
    prompt: str | None,
    confirm: bool,
    no_default_prompt: bool = False,
) -> None:
    """Core async loop â runs the interactive REPL or single-shot."""
    # Event store
    db_path = resolve_obscura_home() / "events.db"
    store = SQLiteEventStore(db_path)
    sid = session_id or uuid.uuid4().hex
    session_artifact_dir = _session_dir(sid)

    # Load .env best-effort
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except Exception:
        pass

    # Tool / MCP setup
    tools_enabled = tools == "on"
    mcp_configs: list[dict[str, Any]] = []
    mcp_names: list[str] = []
    if tools_enabled:
        mcp_configs, mcp_names = _discover_mcp()

    # Create authenticated user for vector memory + memory tools
    import os
    from obscura.auth.models import AuthenticatedUser

    cli_user = AuthenticatedUser(
        user_id=os.environ.get("USER", "local"),
        email="cli@obscura.local",
        roles=("operator",),
        org_id="local",
        token_type="user",
        raw_token="",
    )

    # Initialize vector memory store
    vector_store = init_vector_store(cli_user)

    # Compose system prompt: default Obscura identity + user prompt + session memory
    from obscura.core.context import load_obscura_memory
    from obscura.core.system_prompts import compose_system_prompt

    include_default = not no_default_prompt
    if os.environ.get("OBSCURA_INCLUDE_DEFAULT_PROMPT", "true").lower() == "false":
        include_default = False

    memory_context = load_obscura_memory(sid, db_path)
    custom_sections: list[str] = [memory_context] if memory_context else []

    # Inject vector memory context at session start
    if vector_store is not None:
        vm_startup = load_startup_memories(vector_store, sid, top_k=3)
        if vm_startup:
            custom_sections.append(vm_startup)

    combined_system = compose_system_prompt(
        base=system,
        include_default=include_default,
        custom_sections=custom_sections or None,
    )

    # Gather system tools BEFORE client starts so they're available for
    # tool listing in the system prompt and for the Claude SDK MCP server.
    system_tools: list[Any] = []
    if tools_enabled:
        try:
            from obscura.tools.system import get_system_tool_specs

            system_tools = get_system_tool_specs()
        except Exception:
            pass

        # Add memory tools (semantic_search, store_searchable, etc.)
        if vector_store is not None:
            try:
                from obscura.tools.memory_tools import make_memory_tool_specs

                memory_tools = make_memory_tool_specs(cli_user)
                system_tools.extend(memory_tools)
            except Exception:
                pass

    tool_count = len(system_tools)

    # Load project hooks from .obscura/settings.json + .obscura/hooks/
    from obscura.core.settings import load_all_hooks

    project_hooks = load_all_hooks()

    # Build client (MCP servers connect during start())
    async with ObscuraClient(
        backend,
        model=model,
        system_prompt=combined_system,
        tools=system_tools or None,
        mcp_servers=mcp_configs or None,
        hooks=project_hooks if project_hooks.count > 0 else None,
    ) as client:

        # Session resume
        if session_id:
            try:
                await client.resume_session(
                    SessionRef(session_id=session_id, backend=Backend(backend))
                )
            except Exception:
                pass

        # Build kwargs for run_loop
        loop_kwargs: dict[str, Any] = {}
        if not tools_enabled:
            loop_kwargs["tool_choice"] = ToolChoice.none()

        # Build REPL context
        ctx = REPLContext(
            client=client,
            store=store,
            session_id=sid,
            backend=backend,
            model=model,
            system_prompt=combined_system,
            max_turns=max_turns,
            tools_enabled=tools_enabled,
            log_level=getattr(config, "LOG_LEVEL", "medium"),
            mcp_configs=mcp_configs,
            confirm_enabled=confirm,
            vector_store=vector_store,
        )
        set_secret_menu_visibility(ctx.secret_menu_unlocked)

        # --- Single-shot mode ---
        if prompt:
            try:
                await send_message(ctx, prompt, loop_kwargs)
            finally:
                try:
                    sess = await store.get_session(sid)
                    if sess is not None and sess.status == SessionStatus.RUNNING:
                        await store.update_status(sid, SessionStatus.COMPLETED)
                except Exception:
                    pass
                store.close()
            return

        # --- Interactive REPL ---
        mm = ctx.get_mode_manager()
        toolbar = (
            f"mode: {mm.current.value} \u00b7 esc esc edit previous "
            "\u00b7 esc enter editor \u00b7 /help"
        )
        def _hud_provider() -> dict[str, Any]:
            running_bg = sum(
                1 for t in ctx.background_tasks if t.get("status") == "running"
            )
            running_py = len(ctx._pending_python_tasks)
            return {
                "right_enabled": ctx.ui_right_menu_enabled,
                "model_enabled": ctx.ui_menu_items.get("reasoning", True),
                "menu_items": [
                    ("tasks", f"{running_bg + running_py}" if ctx.ui_menu_items.get("tasks", True) else ""),
                    ("approvals", "on" if (ctx.confirm_enabled and ctx.ui_menu_items.get("approvals", True)) else ""),
                    ("reasoning", "on" if ctx.ui_menu_items.get("reasoning", True) else ""),
                ],
            }

        def _toggle_menu() -> None:
            ctx.ui_right_menu_enabled = not ctx.ui_right_menu_enabled

        def _toggle_item(name: str) -> None:
            ctx.ui_menu_items[name] = not ctx.ui_menu_items.get(name, True)

        session = create_prompt_session(
            COMPLETIONS,
            toolbar_text=toolbar,
            hud_provider=_hud_provider,
            hud_actions={
                "toggle_menu": _toggle_menu,
                "toggle_tasks": lambda: _toggle_item("tasks"),
                "toggle_approvals": lambda: _toggle_item("approvals"),
                "toggle_reasoning": lambda: _toggle_item("reasoning"),
            },
        )
        print_banner(
            backend,
            model,
            sid,
            tool_count=tool_count,
            mcp_servers=mcp_names or None,
            mode=mm.current.value,
        )

        try:
            from obscura.cli import render as render_mod

            render_mod.output.configure_session_log_path(session_artifact_dir)
            render_mod.output.capture_internal(
                f"INFO session_artifacts={session_artifact_dir}"
            )
        except Exception:
            pass

        background_tasks: set[asyncio.Task] = set()
        try:
            while True:
                try:
                    user_input = await bordered_prompt(session)
                except (EOFError, KeyboardInterrupt):
                    console.print()
                    break
                if not user_input:
                    continue

                # Slash command
                if user_input.startswith("/"):
                    if not ctx.tools_enabled:
                        loop_kwargs["tool_choice"] = ToolChoice.none()
                    else:
                        loop_kwargs.pop("tool_choice", None)

                    result = await handle_command(user_input, ctx)
                    if result == "quit":
                        break
                    continue

                # @ command â natural agent routing
                if user_input.startswith("@"):
                    from obscura.cli.commands import cmd_at
                    await cmd_at(user_input[1:], ctx)
                    continue

                # Rebuild loop_kwargs in case tools were toggled
                if not ctx.tools_enabled:
                    loop_kwargs["tool_choice"] = ToolChoice.none()
                else:
                    loop_kwargs.pop("tool_choice", None)

                # Chat message: run send_message in background so REPL remains responsive.
                # Keep Rich spinners disabled here; prompt_toolkit owns cursor rendering.
                task = asyncio.create_task(
                    send_message(
                        ctx,
                        user_input,
                        loop_kwargs,
                        external_status=None,
                        spinner_enabled=False,
                    )
                )
                background_tasks.add(task)
                ctx.task_seq += 1
                task_row: dict[str, str] = {
                    "id": f"bg-{ctx.task_seq}",
                    "status": "running",
                    "kind": "chat",
                    "preview": user_input[:100] + ("..." if len(user_input) > 100 else ""),
                    "started_at": str(time.monotonic()),
                }
                ctx.background_tasks.append(task_row)
                ctx._background_task_refs[task_row["id"]] = task
                if len(ctx.background_tasks) > 50:
                    ctx.background_tasks = ctx.background_tasks[-50:]

                # Remove task from set when done
                def _on_done(t: asyncio.Task) -> None:
                    try:
                        background_tasks.discard(t)
                    except Exception:
                        pass
                    if t.cancelled():
                        task_row["status"] = "cancelled"
                        ctx._background_task_refs.pop(task_row["id"], None)
                        return
                    exc = t.exception()
                    if exc is not None:
                        task_row["status"] = "error"
                        task_row["preview"] = (
                            f"{task_row['preview']} | {str(exc)[:80]}"
                        )
                        try:
                            console.print(
                                f"[red]Background task {task_row['id']} failed:[/] "
                                f"{str(exc)[:200]}"
                            )
                        except Exception:
                            pass
                    else:
                        task_row["status"] = "done"
                    ctx._background_task_refs.pop(task_row["id"], None)

                task.add_done_callback(_on_done)

        finally:
            # Wait for any streaming tasks to finish before shutdown
            if background_tasks:
                await asyncio.gather(*background_tasks, return_exceptions=True)
            await ctx.stop_runtime()
            try:
                sess = await store.get_session(sid)
                if sess is not None and sess.status == SessionStatus.RUNNING:
                    await store.update_status(sid, SessionStatus.COMPLETED)
            except Exception:
                pass
            store.close()


# ---------------------------------------------------------------------------
# Click entry point
# ---------------------------------------------------------------------------


@click.command()
@click.argument("prompt", required=False, default=None)
@click.option(
    "-b",
    "--backend",
    default="copilot",
    type=click.Choice(["copilot", "claude", "codex"]),
    help="Backend to use.",
)
@click.option("-m", "--model", default=None, help="Model ID override.")
@click.option("-s", "--system", default="", help="System prompt.")
@click.option("--session", default=None, help="Resume session by ID.")
@click.option("--max-turns", default=10, type=int, help="Max agent loop turns.")
@click.option(
    "--tools",
    default="on",
    type=click.Choice(["on", "off"]),
    help="Enable/disable tool calling.",
)
@click.option(
    "--confirm/--no-confirm",
    default=False,
    help="Require approval before each tool call.",
)
@click.option(
    "--no-default-prompt",
    is_flag=True,
    default=False,
    help="Skip the default Obscura system prompt.",
)
def main(
    prompt: str | None,
    backend: str,
    model: str | None,
    system: str,
    session: str | None,
    max_turns: int,
    tools: str,
    confirm: bool,
    no_default_prompt: bool,
) -> None:
    """Obscura â AI agent REPL."""
    try:
        asyncio.run(
            _repl(backend, model, system, session, max_turns, tools, prompt, confirm, no_default_prompt)
        )
    except KeyboardInterrupt:
        pass
