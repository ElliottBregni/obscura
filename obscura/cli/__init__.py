"""obscura.cli — Claude Code-style REPL for Obscura.

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
import uuid
from pathlib import Path
from typing import Any

import click

from obscura.cli.commands import (
    COMPLETIONS,
    REPLContext,
    _FILE_WRITE_TOOLS,
    handle_command,
)
from obscura.cli.prompt import (
    bordered_prompt,
    confirm_prompt_async,
    create_prompt_session,
)
from obscura.cli.render import (
    StreamRenderer,
    console,
    print_banner,
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
    if tool_name in ctx.confirm_always:
        return True

    console.print(f"\n[yellow]Tool:[/] [bold]{tool_name}[/]")
    for k, v in tool_input.items():
        sv = str(v)
        if len(sv) > 80:
            sv = sv[:77] + "..."
        console.print(f"  [dim]{k}=[/]{sv}")

    answer = await confirm_prompt_async("Allow? [y/n/always] ")

    if answer == "always":
        ctx.confirm_always.add(tool_name)
        return True
    return answer in ("y", "yes")


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

    from obscura.cli.app.modes import TUIMode

    if mm.current != TUIMode.PLAN:
        return

    if not response_text.strip():
        return

    from obscura.cli.app.modes import Plan

    plan = Plan.parse(response_text)
    if plan.steps:
        mm.active_plan = plan
        render_plan(plan)


# ---------------------------------------------------------------------------
# Chat message dispatch
# ---------------------------------------------------------------------------


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
    renderer = StreamRenderer(external_status=external_status)
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

    # ── Token-aware auto-compact ────────────────────────────────────────────
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

    if _pre_tokens > _compact_threshold:
        console.print(
            f"[yellow]⚡ Auto-compacting context (~{_pre_tokens:,} tokens, "
            f"60% of {_context_window:,}) …[/]"
        )
        await cmd_compact("6", ctx)

    # ── Vector memory pre-search ──────────────────────────────────────────
    augmented_text = text
    if ctx.vector_store is not None:
        vm_context = search_relevant_context(ctx.vector_store, text, top_k=3)
        if vm_context:
            augmented_text = f"{vm_context}\n\n---\n\n{text}"

    # ── Streaming with graceful retry on context-limit errors ────────────────
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
                    _buf.append(event.text)
                _track_file_event(event.kind, ctx, event)
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
                    "[red]⚠ Context limit reached — aggressive compact and retry…[/]"
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

    # ── Vector memory auto-save ───────────────────────────────────────────
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

    # Parse plan if in PLAN mode
    _maybe_parse_plan(response_text, ctx)

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
    """Core async loop — runs the interactive REPL or single-shot."""
    # Event store
    db_path = resolve_obscura_home() / "events.db"
    store = SQLiteEventStore(db_path)
    sid = session_id or uuid.uuid4().hex

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

    # Load project hooks from .obscura/settings.json and .obscura/hooks/
    project_hooks = None
    try:
        from obscura.core.settings import load_all_hooks

        _hook_registry = load_all_hooks()
        if _hook_registry.count > 0:
            project_hooks = _hook_registry
    except Exception:
        pass

    # Build client (MCP servers connect during start())
    async with ObscuraClient(
        backend,
        model=model,
        system_prompt=combined_system,
        tools=system_tools or None,
        mcp_servers=mcp_configs or None,
        hooks=project_hooks,
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
            mcp_configs=mcp_configs,
            confirm_enabled=confirm,
            vector_store=vector_store,
        )

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
        toolbar = f"mode: {mm.current.value} · esc+enter multiline · /help"
        session = create_prompt_session(COMPLETIONS, toolbar_text=toolbar)
        print_banner(
            backend,
            model,
            sid,
            tool_count=tool_count,
            mcp_servers=mcp_names or None,
            mode=mm.current.value,
        )

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

                # Rebuild loop_kwargs in case tools were toggled
                if not ctx.tools_enabled:
                    loop_kwargs["tool_choice"] = ToolChoice.none()
                else:
                    loop_kwargs.pop("tool_choice", None)

                # Chat message: run send_message in background so REPL remains responsive.
                status = console.status("[dim]› Streaming response...[/]", spinner="dots")
                try:
                    status.start()
                except Exception:
                    pass

                task = asyncio.create_task(send_message(ctx, user_input, loop_kwargs, external_status=status))
                background_tasks.add(task)

                # Remove task from set when done
                def _on_done(t: asyncio.Task) -> None:
                    try:
                        background_tasks.discard(t)
                    except Exception:
                        pass
                    try:
                        status.stop()
                    except Exception:
                        pass

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
    """Obscura — AI agent REPL."""
    try:
        asyncio.run(
            _repl(backend, model, system, session, max_turns, tools, prompt, confirm, no_default_prompt)
        )
    except KeyboardInterrupt:
        pass
