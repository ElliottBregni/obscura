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

# Import-safe public API: keep this very small so tests can import the package
# without triggering heavy runtime subsystems. Use obscura.cli.api for the
# stable surface; more exports may be added to compat layers during refactor.
from . import api as api  # noqa: E402

__all__ = ["api"]










import asyncio
import logging
import time
import uuid
from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    # Type-only imports to keep runtime imports cheap; these names are used
    # throughout this god-module but importing them at runtime recreates the
    # import-time cycles the refactor is trying to eliminate.
    from .commands import (
        REPLContext,
        _FILE_WRITE_TOOLS,
        COMPLETIONS,
        handle_command,
    )
    from .render import (
        console,
        render_plan,
        print_ok,
        print_warning,
        print_banner,
    )
    from .bootstrap import _discover_agent_infos
    from obscura.core.types import AgentEventKind

import click

_log = logging.getLogger("obscura.cli")


def _sync_guide_files() -> None:
    """Keep OBSCURA.md and CLAUDE.md in sync at startup.

    Rules:
      - OBSCURA.md exists → overwrite CLAUDE.md with its content.
      - OBSCURA.md missing, CLAUDE.md exists → create OBSCURA.md from CLAUDE.md.
      - Neither exists → no-op.

    Only operates on the current working directory.  Failures are
    silently logged — this must never block startup.
    """
    cwd = Path.cwd()
    obscura_md = cwd / "OBSCURA.md"
    claude_md = cwd / "CLAUDE.md"

    try:
        if obscura_md.is_file():
            content = obscura_md.read_text(encoding="utf-8")
            claude_md.write_text(content, encoding="utf-8")
            _log.debug("Synced CLAUDE.md ← OBSCURA.md")
        elif claude_md.is_file():
            content = claude_md.read_text(encoding="utf-8")
            obscura_md.write_text(content, encoding="utf-8")
            _log.debug("Created OBSCURA.md ← CLAUDE.md")
    except OSError as exc:
        _log.debug("Guide file sync failed: %s", exc)


def _sync_provider_settings() -> None:
    """Write provider-specific settings to disable their permission layers.

    Obscura has its own tool-policy engine (``obscura/tools/policy/``).
    When running inside a provider like Claude Code, the provider's
    sandbox adds a *second* permission layer that duplicates — and often
    blocks — operations Obscura has already authorised.

    This function writes a provider-local settings file that fully
    disables the outer permission layer so Obscura's policy engine is the
    single source of truth.  Currently supports:

      - **Claude Code** → ``.claude/settings.local.json``

    The file is ``.local.json`` (gitignored by convention), so it never
    leaks into the repo.  Failures are silently logged.
    """
    import json

    cwd = Path.cwd()

    # --- Claude Code --------------------------------------------------
    claude_dir = cwd / ".claude"
    settings_file = claude_dir / "settings.local.json"

    desired: dict[str, Any] = {
        "permissions": {
            "allow": [
                "Bash(*)",
                "Read(*)",
                "Write(*)",
                "Edit(*)",
                "Glob(*)",
                "Grep(*)",
                "WebFetch(*)",
                "WebSearch(*)",
                "Skill(*)",
                "mcp__*",
            ],
            "deny": [],
            "defaultMode": "bypassPermissions",
        },
        "skipDangerousModePermissionPrompt": True,
    }

    try:
        # Merge with any existing settings the user may have added
        existing: dict[str, Any] = {}
        if settings_file.is_file():
            existing = json.loads(settings_file.read_text(encoding="utf-8"))

        # Overlay our permissions but preserve other user keys
        merged = {**existing, **desired}
        # Preserve user allow rules that aren't already covered
        if "permissions" in existing:
            user_allow = existing["permissions"].get("allow", [])
            our_allow = list(desired["permissions"]["allow"])
            for rule in user_allow:
                if rule not in our_allow:
                    our_allow.append(rule)
            merged["permissions"] = {
                **existing.get("permissions", {}),
                **desired["permissions"],
                "allow": our_allow,
            }

        claude_dir.mkdir(exist_ok=True)
        settings_file.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
        _log.debug("Wrote Claude Code bypass settings → %s", settings_file)
    except OSError as exc:
        _log.debug("Provider settings sync failed (claude): %s", exc)


_session_state: dict[str, bool] = {"titled": False}


def _swallow(label: str, exc: Exception) -> None:
    """Log a swallowed exception at DEBUG level instead of silently ignoring."""
    _log.debug("%s: %s: %s", label, type(exc).__name__, exc)


import contextlib  # noqa: E402

from obscura.cli import trace as trace_mod  # noqa: E402

# ---------------------------------------------------------------------------
# MCP / agent discovery — canonical implementations live in bootstrap.py
# ---------------------------------------------------------------------------
# Bootstrap helpers imported lazily to avoid circular import during submodule imports
# CLI command helpers imported lazily to avoid circular import during package import
# Prompt utilities moved to runtime import points to keep package import cheap.
# Import at call sites instead of at module import time.
# Provide a tiny lazy compatibility wrapper so tests can import _discover_mcp from
# obscura.cli without reintroducing earlier circular-imports. The wrapper imports
# bootstrap on call instead of at module import time.

# render helpers imported lazily to avoid circular imports at package import time
from obscura.cli.vector_memory_bridge import (  # noqa: E402  # noqa: E402
    auto_save_turn,
    init_vector_store,
    load_startup_memories,
    run_startup_maintenance,
    search_relevant_context,
    search_with_router,
)
from obscura.core.client import ObscuraClient  # noqa: E402  # noqa: E402
from obscura.core.event_store import SessionStatus, SQLiteEventStore  # noqa: E402  # noqa: E402
from obscura.core.paths import resolve_obscura_home, resolve_obscura_specs_dir  # noqa: E402  # noqa: E402
from obscura.core.types import AgentEventKind, Backend, SessionRef, ToolChoice  # noqa: E402  # noqa: E402

# ---------------------------------------------------------------------------
# Tool confirmation callback
# ---------------------------------------------------------------------------


async def _cli_confirm(
    ctx: REPLContext,
    tool_name: str,
    tool_input: dict[str, Any],
) -> bool:
    """Prompt user to approve a tool call via TUI widget. Returns True to allow."""
    if tool_name in ctx.confirm_always:
        return True

    from obscura.cli.widgets import ToolConfirmRequest, confirm_tool

    result = await confirm_tool(
        ToolConfirmRequest(tool_name=tool_name, tool_input=tool_input),
    )

    if result.action == "always_allow":
        ctx.confirm_always.add(tool_name)
        return True
    return result.action == "allow"


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

    elif (
        ev.kind == AgentEventKind.TOOL_RESULT
        and ev.tool_use_id in ctx._pending_file_reads
    ):
        path, before = ctx._pending_file_reads.pop(ev.tool_use_id)
        try:
            after = Path(path).read_text()
        except (FileNotFoundError, OSError):
            after = ""
        if after != before:
            ctx.add_file_change(path, before, after)
            # Record attribution.
            try:
                from obscura.core.commit_attribution import get_attribution_tracker

                added = len(after.splitlines()) - len(before.splitlines())
                if added >= 0:
                    get_attribution_tracker().record_agent_edit(path, lines_added=added)
                else:
                    get_attribution_tracker().record_agent_edit(
                        path,
                        lines_removed=abs(added),
                    )
            except Exception:
                pass
            # Record in file history.
            try:
                from obscura.tools.system.file_state import record_file_access

                record_file_access(Path(path), "edit")
            except Exception:
                pass


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
    streaming_status: StreamingStatus | None = None,
) -> str:
    """Send a chat message and stream the response with Markdown rendering.

    Returns the accumulated assistant text.
    """
    inline_agent_response = await _run_inline_agent_from_mention(ctx, text)
    if inline_agent_response is not None:
        ctx.message_history.append(("user", text))
        if inline_agent_response:
            ctx.message_history.append(("assistant", inline_agent_response))

        if ctx.vector_store is not None and inline_agent_response:
            turn_num = len([m for m in ctx.message_history if m[0] == "user"])
            auto_save_turn(
                ctx.vector_store,
                ctx.session_id,
                text,
                inline_agent_response,
                turn_number=turn_num,
                classifier=ctx._turn_classifier,
            )
        return inline_agent_response

    from obscura.cli.renderer import create_renderer

    renderer = create_renderer(streaming_status=streaming_status)
    # Feed session context into the modern renderer's status bar
    if hasattr(renderer, "set_session_context"):
        _ps = getattr(ctx, "_prompt_status", None)
        renderer.set_session_context(
            title=getattr(_ps, "session_title", "") or "",
            model=ctx.model or "",
            ctx_pct=getattr(_ps, "ctx_pct", 0),
        )
    # Register active renderer so prompt can expand previews while streaming
    try:
        from obscura.cli.render import set_active_renderer

        set_active_renderer(renderer)
    except Exception:
        pass
    accumulated: list[str] = []

    # Build confirm callback with permission mode integration.
    # This callback is called by AgentLoop before every tool execution.
    from obscura.core.types import ToolCallInfo

    async def confirm_cb(tc: ToolCallInfo) -> bool:
        # 1. Check permission mode (dangerous patterns + mode restrictions).
        try:
            from obscura.core.permission_modes import (
                PermissionMode,
                PermissionModeEngine,
            )

            mode_str = getattr(ctx, "_permission_mode", "default")
            engine = PermissionModeEngine(PermissionMode(mode_str))
            decision = engine.evaluate(tc.name, tc.input)
            if not decision.allowed:
                from obscura.cli.render import print_warning

                print_warning(f"Blocked by {mode_str} mode: {tc.name}")
                return False
            if decision.auto_approved:
                return True
        except Exception:
            pass
        # 2. If confirm is enabled, prompt user.
        if ctx.confirm_enabled:
            return await _cli_confirm(ctx, tc.name, tc.input)
        return True

    # ── Token-aware auto-compact ────────────────────────────────────────────
    # Use provider-specific thresholds from ctx.client so Claude (200k),
    # OpenAI (128k/16k), Copilot (128k), and Codex (128k) all get the
    # right limits without hard-coding numbers here.
    from obscura.cli.commands import cmd_compact, estimate_effective_context_tokens

    _context_window = ctx.client.context_window
    _compact_threshold = int(_context_window * 0.60)  # compact at 60%
    _warn_threshold = ctx.client.context_warn_threshold  # 50% of window

    # Update the system tool's token tracker so the LLM can introspect
    from obscura.tools.system import update_token_usage

    # ── Vector memory pre-search ──────────────────────────────────────────
    augmented_text = text
    slash_skill_context = ctx.build_active_skill_context()
    if slash_skill_context:
        augmented_text = f"{slash_skill_context}\n\n---\n\n{augmented_text}"

    if ctx.vector_store is not None:
        if ctx._context_router is not None:
            vm_context = search_with_router(ctx._context_router, text)
        else:
            vm_context = search_relevant_context(ctx.vector_store, text, top_k=3)
        if vm_context:
            augmented_text = f"{vm_context}\n\n---\n\n{augmented_text}"

    _pre_tokens = estimate_effective_context_tokens(
        ctx,
        pending_user_text=augmented_text,
    )
    update_token_usage(
        input_tokens=_pre_tokens,
        context_window=_context_window,
        compact_threshold=_compact_threshold,
    )
    _stream_output_chars = 0
    _stream_output_tokens_sent = 0
    _last_usage_push = 0.0

    def _push_stream_token_usage(force: bool = False) -> None:
        """Keep context_window_status fresh while streaming text."""
        nonlocal _stream_output_tokens_sent, _last_usage_push
        est_output_tokens = _stream_output_chars // 4
        now = time.monotonic()
        if not force:
            if est_output_tokens - _stream_output_tokens_sent < 32:
                return
            if now - _last_usage_push < 0.75:
                return
        update_token_usage(
            input_tokens=_pre_tokens,
            output_tokens=est_output_tokens,
            context_window=_context_window,
            compact_threshold=_compact_threshold,
        )
        _stream_output_tokens_sent = est_output_tokens
        _last_usage_push = now

    if _pre_tokens > _compact_threshold:
        console.print(
            f"[yellow]⚡ Auto-compacting context (~{_pre_tokens:,} tokens, "
            f"60% of {_context_window:,}) …[/]",
        )
        await cmd_compact("6", ctx)

    _tip_scheduler = (
        None  # initialised later; declared here so nested fn can reference it
    )

    # ── Streaming with graceful retry on context-limit errors ────────────────
    async def _stream_with_retry(
        context_retry_used: bool = False,
        dead_session_retry_used: bool = False,
    ) -> list[str]:
        nonlocal _stream_output_chars
        _buf: list[str] = []
        # Inject effort-level thinking budget if set.
        _effective_kwargs = dict(loop_kwargs)
        if hasattr(ctx, "_effort_level") and ctx._effort_level:
            try:
                from obscura.core.types import EFFORT_THINKING_BUDGETS, EffortLevel

                _lvl = EffortLevel(ctx._effort_level)
                _effective_kwargs["max_thinking_tokens"] = EFFORT_THINKING_BUDGETS[_lvl]
            except (ValueError, KeyError):
                pass
        _s = ctx.client.run_loop(
            augmented_text,
            max_turns=ctx.max_turns,
            event_store=ctx.store,
            session_id=ctx.session_id,
            auto_complete=False,
            on_confirm=confirm_cb,
            **_effective_kwargs,
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
                        [event.tool_name] if getattr(event, "tool_name", None) else []
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
                    _stream_output_chars += len(event.text)
                    _push_stream_token_usage()
                _track_file_event(event.kind, ctx, event)
                # Deep logging: log every tool call and API event.
                try:
                    from obscura.core.deep_log import dlog

                    if event.kind == AgentEventKind.TOOL_CALL:
                        dlog.tool_call(
                            getattr(event, "tool_name", ""),
                            getattr(event, "tool_input", {}),
                        )
                    elif event.kind == AgentEventKind.TOOL_RESULT:
                        dlog.tool_call(
                            getattr(event, "tool_name", ""),
                            ok=not getattr(event, "is_error", False),
                            result_preview=str(getattr(event, "tool_result", ""))[:200],
                        )
                except Exception:
                    pass
                # Tool output collapsing: group consecutive read/search calls.
                if event.kind == AgentEventKind.TOOL_CALL:
                    tool_name = getattr(event, "tool_name", "")
                    tool_input = getattr(event, "tool_input", {})
                    try:
                        from obscura.cli.tool_collapse import ToolCollapser

                        if not hasattr(ctx, "_collapser"):
                            ctx._collapser = ToolCollapser()
                        ctx._collapser.record(tool_name, tool_input)
                    except Exception:
                        pass
                    # Tips: record tool type for targeted tips.
                    if _tip_scheduler is not None:
                        _FILE_TOOLS = {
                            "write_text_file",
                            "edit_text_file",
                            "append_text_file",
                        }
                        _SEARCH_TOOLS = {"grep_files", "find_files", "web_search"}
                        if tool_name in _FILE_TOOLS:
                            _tip_scheduler.record_edit()
                        elif tool_name in _SEARCH_TOOLS:
                            _tip_scheduler.record_search()
                elif event.kind == AgentEventKind.TEXT_DELTA:
                    # Flush collapsed tools before text output.
                    collapser = getattr(ctx, "_collapser", None)
                    if collapser is not None and collapser.pending:
                        try:
                            summary = collapser.flush_summary()
                            if summary:
                                console.print(f"[dim]  {summary}[/]")
                        except Exception:
                            pass
                # Track costs on turn completion.
                if event.kind in (
                    AgentEventKind.TURN_COMPLETE,
                    AgentEventKind.AGENT_DONE,
                ):
                    meta = getattr(event, "metadata", None)
                    if meta is not None:
                        # StreamMetadata stores usage in .usage dict, not direct attributes.
                        _usage = getattr(meta, "usage", None) or {}
                        if isinstance(_usage, dict):
                            inp = _usage.get("input_tokens", 0) or 0
                            out = _usage.get("output_tokens", 0) or 0
                        else:
                            inp = getattr(_usage, "input_tokens", 0) or 0
                            out = getattr(_usage, "output_tokens", 0) or 0
                        if inp > 0 or out > 0:
                            try:
                                from obscura.core.cost_tracker import get_cost_tracker

                                get_cost_tracker().record(
                                    inp,
                                    out,
                                    ctx.model or ctx.backend,
                                )
                            except Exception:
                                pass
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
            _is_dead_session_err = any(
                kw in _err
                for kw in (
                    "dead process",
                    "cannot send message",
                    "can't send message",
                    "cannot write to terminated",
                    "write to terminated process",
                    "terminated process",
                    "exit code",
                    "session is closed",
                    "session closed",
                )
            )
            if _is_ctx_err and not context_retry_used:
                console.print(
                    "[red]⚠ Context limit reached — aggressive compact and retry…[/]",
                )
                await cmd_compact("2", ctx)
                return await _stream_with_retry(
                    context_retry_used=True,
                    dead_session_retry_used=dead_session_retry_used,
                )
            if _is_dead_session_err and not dead_session_retry_used:
                console.print(
                    "[yellow]⚠ Backend session became stale — recreating and retrying once…[/]",
                )
                try:
                    await ctx.client.reset_session()
                except Exception:
                    # reset_session can't revive a dead process;
                    # full recreate is needed
                    with contextlib.suppress(Exception):
                        await ctx.recreate_client(ctx.backend, ctx.model)
                return await _stream_with_retry(
                    context_retry_used=context_retry_used,
                    dead_session_retry_used=True,
                )
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
            classifier=ctx._turn_classifier,
        )

    # Post-send: update token tracker and show nudge
    _push_stream_token_usage(force=True)
    _post_tokens = estimate_effective_context_tokens(ctx)
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
            f"Auto-compact at {_compact_threshold:,} (60%).[/]",
        )

    # Auto-compact: trigger if context exceeds 80% of window.
    if _post_tokens > _compact_threshold:
        try:
            from obscura.core.compaction import should_auto_compact

            if should_auto_compact(
                [{"role": r, "content": t} for r, t in ctx.message_history],
                ctx.model or "default",
                system_prompt=ctx.system_prompt,
            ):
                console.print("[dim cyan]  Auto-compacting context...[/]")
                from obscura.cli.commands import cmd_compact

                await cmd_compact("4", ctx)
        except Exception:
            pass

    # Auto-title: generate session title after first exchange.
    if not _session_state["titled"] and len(ctx.message_history) >= 2:
        _session_state["titled"] = True
        try:
            from obscura.core.session_utils import generate_session_title

            title = await generate_session_title(text, ctx.client._backend)
            if title:
                await ctx.store.update_session(ctx.session_id, summary=title)
                # Update the prompt status so the title appears in the banner/toolbar
                if hasattr(ctx, "_prompt_status") and ctx._prompt_status is not None:
                    ctx._prompt_status.session_title = title
                # Show a subtle notification
                console.print(
                    f"  [dim]session titled:[/] [bold bright_cyan]{title}[/]",
                    highlight=False,
                )
        except Exception:
            pass

    # Parse plan if in PLAN mode
    _maybe_parse_plan(response_text, ctx)

    # Auto-detect question choices and present interactive widget.
    # Skip if the ask_user tool already presented a widget this turn.
    try:
        from obscura.tools.system import reset_ask_user_called, was_ask_user_called

        _tool_asked = was_ask_user_called()
        reset_ask_user_called()

        if not _tool_asked:
            from obscura.cli.widgets import (
                detect_question_choices,
                present_detected_choices,
            )

            detected = detect_question_choices(response_text)
            if detected is not None:
                selection = await present_detected_choices(detected)
                if selection is not None:
                    # Feed the selection back as a user message
                    return await send_message(
                        ctx,
                        selection,
                        loop_kwargs,
                        streaming_status,
                    )
    except Exception:
        pass

    return response_text


# ---------------------------------------------------------------------------
# iMessage daemon
# ---------------------------------------------------------------------------


async def _start_imessage_daemon(
    client: Any,
) -> asyncio.Task[None] | None:
    """Start iMessage daemon if configured in agents.yaml. Returns the task."""
    from obscura.agent.daemon_agent import DaemonAgent
    from obscura.agent.interaction import InteractionBus
    from obscura.agent.supervisor import SupervisorConfig
    from obscura.cli.render import console as _console
    from obscura.core.client import ObscuraClient  # noqa: E402  # noqa: E402

    config_path = Path.home() / ".obscura" / "agents.yaml"
    if not config_path.exists():
        return None

    cfg = SupervisorConfig.from_yaml(config_path)
    # Find daemon agents with iMessage triggers
    for agent_def in cfg.agents:
        if agent_def.type != "daemon":
            continue
        im_triggers = [t for t in agent_def.triggers if t.imessage is not None]
        if not im_triggers:
            continue

        from obscura.agent.daemon_agent import IMessageTrigger as _IMT

        triggers: list[Any] = []
        for tdef in im_triggers:
            im_cfg = tdef.imessage or {}
            im_data = {
                k: v
                for k, v in im_cfg.items()
                if k not in {"contacts", "poll_interval"}
            }
            triggers.append(
                _IMT(
                    contacts=tuple(im_cfg.get("contacts", [])),
                    poll_interval=im_cfg.get("poll_interval", 30),
                    notify_user=tdef.notify_user,
                    priority=tdef.priority,
                    data=im_data,
                ),
            )

        bus = InteractionBus()

        async def _on_output(output: Any) -> None:
            text = getattr(output, "text", str(output))
            source = getattr(output, "source", agent_def.name)
            _console.print(f"[dim]\\[{source}][/] {text}")

        bus.on_output(_on_output)

        # Suppress daemon startup logs — the bottom toolbar shows daemon
        # status, so raw StreamHandler output would corrupt the prompt UI.
        import logging as _logging

        _logging.getLogger("obscura.agent.daemon_agent").setLevel(_logging.WARNING)

        # Create a SEPARATE client for the daemon so it doesn't contend
        # with the REPL's client for backend access
        daemon_client = ObscuraClient(
            agent_def.model,
            system_prompt=agent_def.system_prompt,
        )
        await daemon_client.__aenter__()

        # Load persisted schedules from ~/.obscura/schedules.json
        try:
            from obscura.agent.daemon_agent import ScheduleTrigger as _ST

            _schedules_path = Path.home() / ".obscura" / "schedules.json"
            if _schedules_path.is_file():
                import json as _sched_json

                for sched in _sched_json.loads(
                    _schedules_path.read_text(encoding="utf-8"),
                ):
                    triggers.append(
                        _ST(
                            cron=sched["cron"],
                            prompt=sched["prompt"],
                            description=f"{sched.get('id', '?')}: {sched['prompt'][:40]}",
                            notify_user=bool(sched.get("notify", True)),
                        ),
                    )
        except Exception as _sched_exc:
            _log.debug("Failed to load persisted schedules: %s", _sched_exc)

        daemon = DaemonAgent(daemon_client, name=agent_def.name, triggers=triggers)
        daemon._bus = bus
        task: asyncio.Task[None] = asyncio.create_task(
            daemon.loop_forever(),  # type: ignore[arg-type]
            name=f"daemon-{agent_def.name}",
        )

        def _on_task_done(t: asyncio.Task[None]) -> None:  # type: ignore[type-arg]
            exc = t.exception() if not t.cancelled() else None
            if exc:
                _console.print(f"[red]Daemon task crashed: {exc}[/]")
            elif t.cancelled():
                _console.print("[dim]Daemon task cancelled[/]")
            else:
                _console.print("[dim]Daemon task completed[/]")

        task.add_done_callback(_on_task_done)
        # Stash client on the task so we can close it later
        task._daemon_client = daemon_client
        return task

    return None


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
    *,
    supervise: bool = True,
    compiled_ws: Any | None = None,
) -> None:
    """Core async loop — runs the interactive REPL or single-shot."""
    # Event store
    db_path = resolve_obscura_home() / "events.db"
    store = SQLiteEventStore(db_path)
    sid = session_id or uuid.uuid4().hex

    # Resolve backend/model names from arguments or environment defaults
    import os

    backend_name = backend or os.environ.get("OBSCURA_BACKEND", "")
    model_name = model or os.environ.get("OBSCURA_MODEL", "")

    # Load .env best-effort — global first, then project-local overlay.
    # load_dotenv(override=False) never overwrites already-set vars, so
    # ordering is: shell env > global ~/.obscura/.env > project .obscura/.env > CWD .env
    try:
        from dotenv import load_dotenv

        from obscura.core.paths import resolve_obscura_global_home

        # 1. Always load the global ~/.obscura/.env (user-wide creds/keys)
        global_env = resolve_obscura_global_home() / ".env"
        if global_env.is_file():
            load_dotenv(global_env)

        # 2. Load project-local .obscura/.env if it differs from global
        local_env = resolve_obscura_home() / ".env"
        if local_env.is_file() and local_env.resolve() != global_env.resolve():
            load_dotenv(local_env)

        # 3. CWD .env (won't overwrite already-set vars)
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

    # Run decay maintenance (purge expired, consolidate old episodes) in background
    if vector_store is not None:
        run_startup_maintenance(vector_store)

    # Initialize memory channel router (dynamic context injection)
    _context_router = None
    _turn_classifier = None
    if vector_store is not None:
        try:
            from obscura.memory_channels import (
                ContextRouter,
                TurnClassifier,
                load_channels_from_config,
            )

            _channels = load_channels_from_config()
            if _channels:
                _context_router = ContextRouter(_channels, vector_store)
                _turn_classifier = TurnClassifier(_channels)
        except Exception:
            pass

    # Compose system prompt: default Obscura identity + user prompt + session memory
    from obscura.core.context import load_obscura_memory
    from obscura.core.system_prompts import (
        compose_environment_context,
        compose_system_prompt,
    )

    include_default = not no_default_prompt
    if os.environ.get("OBSCURA_INCLUDE_DEFAULT_PROMPT", "true").lower() == "false":
        include_default = False

    memory_context = load_obscura_memory(sid, db_path)
    custom_sections: list[str] = [memory_context] if memory_context else []

    # Inject user identity & preferences from preferences.md
    prefs_path = resolve_obscura_home() / "memory" / "preferences.md"
    if prefs_path.exists():
        prefs_text = prefs_path.read_text().strip()
        if prefs_text:
            custom_sections.append(f"# User Identity & Preferences\n\n{prefs_text}")

    # Inject vector memory context at session start
    if vector_store is not None:
        vm_startup = load_startup_memories(vector_store, sid, top_k=3)
        if vm_startup:
            custom_sections.append(vm_startup)

    # Inject memory channel documentation and system-level channel context
    if _context_router is not None:
        try:
            from obscura.tools.memory_tools import build_channels_prompt_section

            channels_doc = build_channels_prompt_section(_context_router.channels)
            if channels_doc:
                custom_sections.append(channels_doc)

            sys_channel_ctx = _context_router.get_system_channels()
            if sys_channel_ctx:
                custom_sections.append(sys_channel_ctx)
        except Exception:
            pass

    # Inject environment context (available plugins, capabilities, agent types)
    try:
        from obscura.agent import AGENT_TYPE_REGISTRY
        from obscura.plugins.builtins import list_builtin_plugin_ids

        env_section = compose_environment_context(
            plugin_ids=list_builtin_plugin_ids(),
            capabilities=[
                "shell.exec",
                "file.read",
                "file.write",
                "git.ops",
                "web.browse",
                "search.web",
                "security.scan",
            ],
            agent_types=list(AGENT_TYPE_REGISTRY.keys()),
        )
        if env_section:
            custom_sections.append(env_section)
    except Exception:
        pass  # graceful degradation

    # Inject KAIROS context into system prompt if enabled
    try:
        from obscura.kairos.engine import KairosEngine as _KairosEngineProbe
        from obscura.kairos.engine import is_kairos_enabled as _kep

        if _kep():
            _probe_engine = _KairosEngineProbe()
            _kairos_sys = _probe_engine.get_system_prompt_addition()
            if _kairos_sys:
                custom_sections.append(_kairos_sys)
    except Exception:
        pass

    # Inject coordinator system prompt when coordinator mode is active
    try:
        from obscura.agent.coordinator import (
            get_coordinator_system_prompt,
            is_coordinator_mode,
        )

        if is_coordinator_mode():
            custom_sections.append(get_coordinator_system_prompt())

            # Inject agent catalog so the LLM knows available agents + tags
            try:
                from obscura.tools.swarm import build_agent_catalog, load_agent_configs

                catalog = build_agent_catalog(load_agent_configs())
                if catalog:
                    custom_sections.append(
                        f"## Available Specialist Agents\n\n{catalog}"
                    )
            except Exception:
                pass
    except Exception:
        pass

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

        # Add worktree tools (enter_worktree, exit_worktree)
        try:
            from obscura.tools.worktree import get_worktree_tool_specs

            system_tools.extend(get_worktree_tool_specs())
        except Exception:
            pass

        # Add task management tools (task_create, task_get, task_list, etc.)
        try:
            from obscura.tools.task_tools import get_task_tool_specs

            system_tools.extend(get_task_tool_specs())
        except Exception:
            pass

        # Add goal board tools (goal_create, goal_list, goal_get, etc.)
        try:
            from obscura.tools.goal_tools import get_goal_tool_specs

            system_tools.extend(get_goal_tool_specs())
        except Exception:
            pass

        # Add user profile tools (profile_get, profile_update, profile_recall, profile_sync)
        try:
            from obscura.tools.profile_tools import get_profile_tool_specs

            system_tools.extend(get_profile_tool_specs())
        except Exception:
            pass

        # Add LSP tool (code navigation)
        try:
            from obscura.tools.lsp import get_lsp_tool_specs

            system_tools.extend(get_lsp_tool_specs())
        except Exception:
            pass

        # Add browser automation tool (Playwright)
        try:
            from obscura.tools.browser import get_browser_tool_specs

            system_tools.extend(get_browser_tool_specs())
        except Exception:
            pass

        # Load builtin plugin tools — filtered by workspace packs when available
        try:
            existing_names = {t.name for t in system_tools}
            _ws_include = (
                getattr(compiled_ws, "plugin_include", None) if compiled_ws else None
            )
            _ws_exclude = (
                getattr(compiled_ws, "plugin_exclude", None) if compiled_ws else None
            )

            if _ws_include or _ws_exclude:
                from obscura.plugins.loader import get_filtered_builtin_tool_specs

                plugin_tools = get_filtered_builtin_tool_specs(_ws_include, _ws_exclude)
            else:
                from obscura.plugins.loader import get_all_builtin_tool_specs

                plugin_tools = get_all_builtin_tool_specs()

            for tool in plugin_tools:
                if tool.name not in existing_names:
                    system_tools.append(tool)
                    existing_names.add(tool.name)
        except Exception:
            pass

    # Backfill capability field on system tools from plugin manifests.
    # System tools registered via @tool() don't carry capability metadata,
    # but the plugin manifests (system-tools.toml etc.) declare the mapping.
    if tools_enabled and system_tools:
        try:
            from dataclasses import replace as _dc_replace

            from obscura.plugins.loader import get_capability_map

            _cap_map = get_capability_map()
            system_tools = [
                _dc_replace(t, capability=_cap_map[t.name])
                if not getattr(t, "capability", "") and t.name in _cap_map
                else t
                for t in system_tools
            ]
        except Exception:
            pass

    # Filter tools by capability grants from config.toml.
    # Tools whose capability is not in the grant list are removed.
    # Tools with no capability (uncategorized) are always kept.
    if tools_enabled and system_tools:
        try:
            from obscura.plugins.capabilities import resolve_allowed_tools_from_config

            _allowed = resolve_allowed_tools_from_config()
            if _allowed is not None:
                system_tools = [
                    t
                    for t in system_tools
                    if not getattr(t, "capability", "")  # no capability → keep
                    or t.name in _allowed  # in grant list → keep
                ]
        except Exception:
            pass

    tool_count = len(system_tools)

    # Wire the ask_user callback so the tool can present TUI widgets
    if tools_enabled:
        try:
            from obscura.tools.system import set_ask_user_callback

            async def _ask_user_handler(
                question: str,
                choices: list[str],
                allow_custom: bool = False,
            ) -> str:
                from obscura.cli.widgets import (
                    AttentionWidgetRequest,
                    ModelQuestionRequest,
                    ask_model_question,
                    confirm_attention,
                )

                if choices:
                    result = await confirm_attention(
                        AttentionWidgetRequest(
                            request_id="ask_user",
                            agent_name="assistant",
                            message=question,
                            priority="normal",
                            actions=tuple(choices),
                        ),
                    )
                    return result.action
                result = await ask_model_question(
                    ModelQuestionRequest(question=question),
                )
                return result.text

            set_ask_user_callback(_ask_user_handler)
        except Exception:
            pass

    # Wire the plan-mode toggle so enter_plan_mode / exit_plan_mode tools work
    if tools_enabled:
        try:
            from obscura.tools.system import (
                set_permission_mode_callback,
                set_plan_approval_callback,
            )

            def _set_permission_mode(mode: str) -> None:
                ctx._permission_mode = mode

            set_permission_mode_callback(_set_permission_mode)

            async def _plan_approval_handler(plan_summary: str) -> bool:
                """Gate plan-mode exit on user approval via the renderer."""
                from obscura.cli.widgets import (
                    PermissionWidgetRequest,
                    confirm_permission,
                )

                result = await confirm_permission(
                    PermissionWidgetRequest(
                        action="Exit plan mode and begin implementation",
                        reason=plan_summary or "Agent wants to leave plan mode.",
                        risk="medium",
                    ),
                )
                return result.action == "approve"

            set_plan_approval_callback(_plan_approval_handler)
        except Exception:
            pass

    # Wire the user_interact callback for permission/notify/question modes
    if tools_enabled:
        try:
            from obscura.tools.system import set_user_interact_callback

            async def _user_interact_handler(**kwargs: Any) -> dict[str, Any]:
                mode = kwargs.get("mode", "question")

                if mode == "permission":
                    from obscura.cli.widgets import (
                        PermissionWidgetRequest,
                        confirm_permission,
                    )

                    result = await confirm_permission(
                        PermissionWidgetRequest(
                            action=kwargs.get("action", ""),
                            reason=kwargs.get("reason", ""),
                            risk=kwargs.get("risk", "low"),
                        ),
                    )
                    return {"approved": result.action == "approve"}

                if mode == "notify":
                    from obscura.cli.widgets import (
                        NotifyWidgetRequest,
                        render_notification_banner,
                    )

                    render_notification_banner(
                        NotifyWidgetRequest(
                            title=kwargs.get("title", ""),
                            message=kwargs.get("message", ""),
                            priority=kwargs.get("priority", "normal"),
                        ),
                    )
                    return {}

                if mode == "multi_select":
                    from obscura.cli.widgets import (
                        MultiSelectRequest,
                        ask_multi_select as _ask_multi_select,
                    )

                    choices = kwargs.get("choices", [])
                    question = kwargs.get("question", "")
                    result = await _ask_multi_select(
                        MultiSelectRequest(
                            question=question,
                            choices=tuple(choices),
                        ),
                    )
                    # result.text is comma-separated selections
                    selected = [s.strip() for s in result.text.split(",") if s.strip()]
                    return {"selected": selected}

                # question mode (default)
                from obscura.cli.widgets import (
                    AttentionWidgetRequest,
                    ModelQuestionRequest,
                    ask_model_question,
                    confirm_attention,
                )

                choices = kwargs.get("choices", [])
                question = kwargs.get("question", "")
                if choices:
                    result = await confirm_attention(
                        AttentionWidgetRequest(
                            request_id="user_interact",
                            agent_name="assistant",
                            message=question,
                            priority="normal",
                            actions=tuple(choices),
                        ),
                    )
                    return {"selected": result.action}
                result = await ask_model_question(
                    ModelQuestionRequest(question=question),
                )
                return {"selected": result.text}

            set_user_interact_callback(_user_interact_handler)
        except Exception:
            pass

    # Load project hooks from .obscura/settings.json and .obscura/hooks/
    project_hooks = None
    try:
        from obscura.core.settings import load_all_hooks

        _hook_registry = load_all_hooks()
        if _hook_registry.count > 0:
            project_hooks = _hook_registry
    except Exception:
        pass

    # Wire memory channel TOOL_CALL hook to capture file paths + tool names.
    # Also feeds file_context to the tool router for context-aware tool selection.
    _tool_router_ref = None  # set after client creation
    if _context_router is not None:
        from obscura.core.hooks import HookRegistry
        from obscura.core.types import AgentEventKind as _AEK

        if project_hooks is None:
            project_hooks = HookRegistry()

        def _channel_tool_signal(event: Any) -> None:
            _context_router.update_signals_from_event(event)
            # Sync file paths to tool router for context-aware recall
            if _tool_router_ref is not None and _context_router.signals.file_paths:
                _tool_router_ref.set_file_context(
                    list(_context_router.signals.file_paths),
                )

        project_hooks.add_after(_channel_tool_signal, _AEK.TOOL_CALL)

    # Wire Kairos tool-call and turn-complete logging hooks (closure over _kairos_engine)
    try:
        from obscura.kairos.engine import is_kairos_enabled as _kie2

        if _kie2():
            from obscura.core.hooks import HookRegistry
            from obscura.core.types import AgentEventKind as _AEK2

            if project_hooks is None:
                project_hooks = HookRegistry()

            def _kairos_tool_hook(event: Any) -> None:
                if _kairos_engine is not None and _kairos_engine.is_running:
                    tool = getattr(event, "tool_name", "") or ""
                    args = str(getattr(event, "tool_input", "") or "")[:80]
                    _kairos_engine.log_tool_use(tool, args)

            def _kairos_turn_hook(event: Any) -> None:
                if _kairos_engine is not None and _kairos_engine.is_running:
                    _kairos_engine.log_agent_event("turn_complete")

            project_hooks.add_after(_kairos_tool_hook, _AEK2.TOOL_CALL)
            project_hooks.add_after(_kairos_turn_hook, _AEK2.TURN_COMPLETE)
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
        # Wire eval-driven tool router so the backend selects a relevant
        # subset of tools per turn (pinned core tools + capability matches).
        # Without this, all 100+ tools get sent to the model every turn.
        if tools_enabled:
            try:
                from obscura.core.compiler.compiled import ToolRoutingConfig
                from obscura.core.tool_router import ToolRouter
                from obscura.core.tool_score_index import ToolScoreIndex
                from obscura.plugins.loader import (
                    PluginLoader,
                    _load_plugin_config_flag,
                )
                from obscura.plugins.registries.capability_index import CapabilityIndex

                _routing_config = ToolRoutingConfig()
                _score_index = ToolScoreIndex()

                # Build capability index from loaded plugins
                _cap_index = CapabilityIndex()
                _pl = PluginLoader()
                _all_pspecs = []
                if _load_plugin_config_flag("load_builtins"):
                    _all_pspecs.extend(_pl.discover_builtins())
                _all_pspecs.extend(_pl.discover_local())
                _all_pspecs.extend(_pl.discover_user())
                for _ps in _all_pspecs:
                    for _cap in _ps.capabilities:
                        _cap_index.register(_cap, _ps.id)

                _router = ToolRouter.from_capability_index(
                    config=_routing_config,
                    score_index=_score_index,
                    capability_index=_cap_index,
                    backend=backend,
                )
                client._backend.set_tool_router(_router)
                # Let the TOOL_CALL hook feed file_context to this router
                _tool_router_ref = _router
            except Exception:
                pass

        # Session resume
        if session_id:
            try:
                await client.resume_session(
                    SessionRef(session_id=session_id, backend=Backend(backend)),
                )
            except Exception as exc:
                # Keep the local session timeline but recover backend state.
                print_warning(
                    f"Resume failed for session {session_id[:12]}: {exc}. "
                    "Starting a fresh backend session.",
                )
                with contextlib.suppress(Exception):
                    await client.reset_session()

        # Build kwargs for run_loop
        loop_kwargs: dict[str, Any] = {}
        if not tools_enabled:
            loop_kwargs["tool_choice"] = ToolChoice.none()

        # Wire effort level → thinking budget if set on context.
        def _inject_effort(kwargs: dict[str, Any], ctx_ref: Any) -> dict[str, Any]:
            """Inject max_thinking_tokens from effort level into loop kwargs."""
            effort_val = getattr(ctx_ref, "_effort_level", None)
            if effort_val:
                try:
                    from obscura.core.types import EFFORT_THINKING_BUDGETS, EffortLevel

                    level = EffortLevel(effort_val)
                    kwargs["max_thinking_tokens"] = EFFORT_THINKING_BUDGETS[level]
                except (ValueError, KeyError):
                    pass
            return kwargs

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
            _context_router=_context_router,
            _turn_classifier=_turn_classifier,
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
        # Import prompt utilities lazily to avoid heavy imports at package import time.
        from obscura.cli.prompt import (
            StreamingStatus,
            PromptStatus,
            _get_git_branch,
            animate_spinner,
            bordered_prompt,
            create_prompt_session,
        )

        ss = StreamingStatus()

        # Lazy agent discovery — reads agents.yaml metadata, no runtime created
        agent_infos = _discover_agent_infos()
        available_agents = [a.name for a in agent_infos] or None

        print_banner(
            backend,
            model,
            sid,
            tool_count=tool_count,
            mcp_servers=mcp_names or None,
            mode=mm.current.value,
            available_agents=available_agents,
            agent_infos=agent_infos or None,
        )

        # Start supervisor if --supervise (default) and agents.yaml has agents
        supervisor_task: asyncio.Task[None] | None = None
        _supervisor: Any = None
        if supervise and agent_infos:
            try:
                import os as _os

                from obscura.agent.supervisor import AgentSupervisor
                from obscura.auth.models import AuthenticatedUser

                sup_user = AuthenticatedUser(
                    user_id=_os.environ.get("USER", "local"),
                    email="cli@obscura.local",
                    roles=("operator",),
                    org_id="local",
                    token_type="user",
                    raw_token="",
                )
                agents_yaml = resolve_obscura_home() / "agents.yaml"
                _supervisor = AgentSupervisor(
                    config_path=agents_yaml,
                    user=sup_user,
                )
                supervisor_task = asyncio.create_task(
                    _supervisor.run_forever(),
                    name="supervisor",
                )
                print_ok(f"Supervisor started — {len(agent_infos)} agent(s) launching")
            except Exception as exc:
                print_warning(f"Supervisor failed to start: {exc}")

        # Start iMessage daemon only when the supervisor is NOT running —
        # the supervisor already manages all agents from agents.yaml,
        # including imessage-assistant.  Starting it twice causes a lock
        # contention loop ("another daemon instance owns lock").
        daemon_task: asyncio.Task[None] | None = None
        _daemon_client: Any = None
        if supervisor_task is None:
            try:
                daemon_task = await _start_imessage_daemon(ctx.client)
            except Exception as exc:
                print_warning(f"iMessage daemon failed to start: {exc}")
        daemon_restart_count = 0
        daemon_last_restart_at = 0.0

        # Live status shown in the bottom toolbar — mutated before each prompt
        prompt_status = PromptStatus(
            model=model or "",
            branch=_get_git_branch(),
            session_id=sid,
            mode=mm.current.value,
        )
        # Stash on ctx so auto-title can update it from send_message
        ctx._prompt_status = prompt_status  # type: ignore[attr-defined]

        def _refresh_prompt_status() -> None:
            """Refresh mutable fields of prompt_status before each prompt."""
            from obscura.cli.prompt import RunningAgentInfo

            prompt_status.mode = mm.current.value
            prompt_status.model = ctx.model or ""
            # Collect running agents from runtime (if active)
            running: list[str] = []
            details: list[RunningAgentInfo] = []
            if ctx._runtime is not None:
                try:
                    from datetime import UTC, datetime

                    from obscura.agent.agents import AgentStatus as _AS

                    _active = {_AS.RUNNING, _AS.WAITING, _AS.PENDING}
                    for agent in ctx._runtime.list_agents():
                        if agent.status not in _active:
                            continue
                        running.append(agent.config.name)
                        elapsed = (
                            (datetime.now(UTC) - agent.created_at).total_seconds()
                            if hasattr(agent, "created_at")
                            else 0.0
                        )
                        details.append(
                            RunningAgentInfo(
                                name=agent.config.name,
                                status=agent.status.name.lower(),
                                elapsed_s=elapsed,
                                iteration_count=getattr(agent, "iteration_count", 0),
                                last_tool=getattr(agent, "_last_tool_name", ""),
                            )
                        )
                except Exception:
                    pass
            # Include daemon task if alive
            if daemon_task is not None and not daemon_task.done():
                task_name = daemon_task.get_name()
                label = (
                    task_name.removeprefix("daemon-")
                    if task_name.startswith("daemon-")
                    else task_name
                )
                if label not in running:
                    running.append(label)
                    details.append(RunningAgentInfo(name=label, status="running"))
            prompt_status.running_agents = running
            prompt_status.agent_details = details
            # Count active tasks (agents in non-terminal states + daemon)
            task_count = 0
            if ctx._runtime is not None:
                try:
                    from obscura.agent.agents import AgentStatus as _AS2

                    _active = {_AS2.RUNNING, _AS2.WAITING, _AS2.PENDING}
                    task_count += sum(
                        1 for a in ctx._runtime.list_agents() if a.status in _active
                    )
                except Exception:
                    pass
            if daemon_task is not None and not daemon_task.done():
                task_count += 1
            prompt_status.task_count = task_count
            # Token context tracking
            from obscura.cli.commands import estimate_effective_context_tokens

            tokens = estimate_effective_context_tokens(ctx)
            window = ctx.client.context_window
            prompt_status.ctx_tokens = tokens
            prompt_status.ctx_window = window
            prompt_status.ctx_pct = int(tokens / window * 100) if window else 0

        session = create_prompt_session(
            COMPLETIONS,
            streaming_status=ss,
            prompt_status=prompt_status,
            at_command_names=ctx.discover_at_commands,
            dollar_skill_names=ctx.discover_dollar_skills,
        )

        # Background spinner animation for the toolbar
        spinner_task = asyncio.create_task(animate_spinner(ss))

        # --- KAIROS integration: wire into supervisor or start directly ---
        _kairos_engine = None
        _kairos_hooks_registered = False
        try:
            from obscura.kairos.engine import KairosEngine, is_kairos_enabled

            if is_kairos_enabled():
                _kairos_engine = KairosEngine()
                if _supervisor is not None and hasattr(_supervisor, "hooks"):
                    from obscura.kairos.supervisor_hooks import register_kairos_hooks

                    register_kairos_hooks(_supervisor.hooks, _kairos_engine)
                    _kairos_hooks_registered = True
                else:
                    # No supervisor — start directly (fallback)
                    await _kairos_engine.start()
        except Exception as _e:
            _swallow("kairos_start", _e)

        # Wire the active AgentLoop into KairosEngine for proactive tick injection.
        if _kairos_engine is not None:
            try:
                _agent_loop = getattr(client, "_loop", None)
                if _agent_loop is not None:
                    _kairos_engine.register_agent_loop(_agent_loop)
            except Exception as _e:
                _swallow("kairos_loop_wire", _e)

        # --- Tips scheduler ---
        _tip_scheduler = None
        try:
            from obscura.cli.tips import TipScheduler

            _tip_scheduler = TipScheduler()
        except Exception as _e:
            _swallow("tips_init", _e)

        # --- Frustration detector (kairos subsystem — gated by is_kairos_enabled) ---
        _frustration_detector = None
        try:
            from obscura.kairos.engine import is_kairos_enabled as _kairos_enabled

            if _kairos_enabled():
                from obscura.kairos.frustration import FrustrationDetector

                _frustration_detector = FrustrationDetector()
        except Exception as _e:
            _swallow("frustration_init", _e)

        # --- Away summary tracker (kairos subsystem — gated by is_kairos_enabled) ---
        _away_tracker = None
        try:
            from obscura.kairos.engine import is_kairos_enabled as _kairos_enabled

            if _kairos_enabled():
                from obscura.kairos.away_summary import AwaySummaryTracker

                _away_tracker = AwaySummaryTracker()
        except Exception as _e:
            _swallow("away_init", _e)

        # --- Prompt cache ---
        _prompt_cache = None
        try:
            from obscura.core.prompt_cache import PromptCacheManager

            _prompt_cache = PromptCacheManager()
        except Exception:
            pass

        # --- Register cleanup tasks ---
        try:
            from obscura.core.cleanup import cleanup_stale_files, register_cleanup

            register_cleanup(
                "stale_files",
                lambda: cleanup_stale_files(max_age_days=30),
            )
        except Exception as _e:
            _swallow("cleanup_init", _e)

        # --- Concurrent session detection ---
        try:
            from obscura.core.session_utils import (
                check_concurrent_sessions,
                install_signal_handlers,
                register_session,
                register_shutdown_handler,
                unregister_session,
            )

            register_session(sid, backend=backend_name, model=model_name or "")
            register_shutdown_handler(lambda: unregister_session(sid))
            install_signal_handlers()
            concurrent = check_concurrent_sessions(sid)
            if concurrent:
                console.print(
                    f"[yellow]Note: {len(concurrent)} other session(s) running in this workspace[/]",
                )
        except Exception:
            pass

        # --- Deep log session start ---
        try:
            from obscura.core.deep_log import dlog

            dlog.session_event(
                "start",
                session_id=sid,
                backend=backend_name,
                model=model_name or "",
            )
        except Exception:
            pass

        # --- UDS inbox for cross-session messaging ---
        _uds_inbox = None
        try:
            from obscura.kairos.uds_messaging import UDSInbox

            _uds_inbox = UDSInbox(sid)

            def _on_peer_message(msg: dict) -> None:
                sender = msg.get("from", "?")
                text = msg.get("text", "")
                console.print(f"\n[bold cyan]Message from {sender}:[/] {text}")

            await _uds_inbox.start(on_message=_on_peer_message)
        except Exception as _e:
            _swallow("uds_init", _e)
            _uds_inbox = None

        # --- Auto-title tracking (mutable container for closure access) ---
        global _session_state
        _session_state = {"titled": False}

        background_tasks: set[asyncio.Task[str]] = set()
        try:
            while True:
                try:
                    if daemon_task is not None and daemon_task.done():
                        now = time.monotonic()
                        # Avoid crash-loop hammering.
                        if now - daemon_last_restart_at >= 5.0:
                            daemon_last_restart_at = now
                            daemon_restart_count += 1
                            exc: Exception | None = None
                            if not daemon_task.cancelled():
                                try:
                                    exc = daemon_task.exception()
                                except Exception:
                                    exc = None
                            dc = getattr(daemon_task, "_daemon_client", None)
                            if dc is not None:
                                with contextlib.suppress(Exception):
                                    await dc.__aexit__(None, None, None)
                            print_warning(
                                "iMessage daemon stopped unexpectedly; restarting "
                                f"(attempt {daemon_restart_count})"
                                + (f": {exc}" if exc else ""),
                            )
                            try:
                                daemon_task = await _start_imessage_daemon(ctx.client)
                            except Exception as restart_exc:
                                print_warning(
                                    f"iMessage daemon restart failed: {restart_exc}",
                                )
                                daemon_task = None
                    _refresh_prompt_status()
                    user_input = await bordered_prompt(session, status=prompt_status)
                except (EOFError, KeyboardInterrupt):
                    console.print()
                    break
                if not user_input:
                    continue

                # Voice input: intercept __VOICE_RECORD__ marker from Ctrl+Space.
                if user_input == "__VOICE_RECORD__":
                    voice_enabled = getattr(ctx, "_voice_enabled", False)
                    if not voice_enabled:
                        console.print(
                            "[dim]Voice mode is off. Enable with /voice on[/]",
                        )
                        continue
                    try:
                        from obscura.voice.session import VoiceSession

                        _vsession = VoiceSession()
                        if not _vsession.is_available:
                            console.print(
                                f"[red]Voice unavailable: {_vsession.install_hint}[/]",
                            )
                            continue
                        console.print(
                            "[yellow]Recording... (speak now, press Enter when done)[/]",
                        )
                        await _vsession.start_recording()
                        # Wait for user to press Enter to stop.
                        with contextlib.suppress(EOFError, KeyboardInterrupt):
                            await bordered_prompt(session)
                        transcript = await _vsession.stop_and_transcribe()
                        if transcript:
                            console.print(f"[green]Voice:[/] {transcript}")
                            user_input = transcript
                        else:
                            console.print("[dim]No speech detected.[/]")
                            continue
                    except Exception as voice_exc:
                        console.print(f"[red]Voice error: {voice_exc}[/]")
                        continue

                # KAIROS: log user message
                if _kairos_engine is not None and _kairos_engine.is_running:
                    with contextlib.suppress(Exception):
                        _kairos_engine.log_user_message(user_input)

                # Keyword detection: "ultrathink" triggers max effort + visual.
                if "ultrathink" in user_input.lower() and not user_input.startswith(
                    "/",
                ):
                    if getattr(ctx, "_effort_level", "medium") != "max":
                        ctx._effort_level = "max"
                        try:
                            from obscura.cli.tui_effects import ultrathink_banner

                            ultrathink_banner()
                        except Exception:
                            console.print(
                                "[bold bright_magenta]⚡ ULTRATHINK activated[/]",
                            )

                # Frustration detection: check user input for frustration signals.
                if _frustration_detector is not None and not user_input.startswith("/"):
                    try:
                        _sentiment = _frustration_detector.analyze(user_input)
                        if (
                            _sentiment.is_frustrated
                            and _sentiment.consecutive_frustrations >= 2
                        ):
                            console.print(
                                "[dim italic]I notice some frustration — "
                                "let me be more careful with my approach.[/]",
                            )
                    except Exception:
                        pass

                # Away summary: mark user as active, show summary if returning.
                if _away_tracker is not None:
                    try:
                        if _away_tracker.should_generate():
                            from obscura.kairos.away_summary import (
                                generate_away_summary,
                            )

                            _summary = await generate_away_summary(ctx.message_history)
                            if _summary:
                                console.print(f"[dim]{_summary}[/]")
                        _away_tracker.mark_active()
                    except Exception:
                        pass

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

                # *eval — benchmark a command/skill chain
                if user_input.startswith("*"):
                    import subprocess as _sp

                    from obscura.cli.render import print_error as _pe
                    from obscura.cli.render import print_info as _pi

                    def _snapshot_git() -> str | None:
                        """Capture current git diff so we can revert on eval failure."""
                        try:
                            r = _sp.run(
                                ["git", "diff", "--name-only", "HEAD"],
                                capture_output=True,
                                text=True,
                                timeout=5,
                            )
                            # Also capture untracked files
                            u = _sp.run(
                                ["git", "ls-files", "--others", "--exclude-standard"],
                                capture_output=True,
                                text=True,
                                timeout=5,
                            )
                            files = (r.stdout.strip() + "\n" + u.stdout.strip()).strip()
                            return files or None
                        except Exception:
                            return None

                    def _revert_changes(before_files: str | None) -> list[str]:
                        """Revert files changed since the snapshot.

                        Returns list of reverted file paths.
                        """
                        try:
                            r = _sp.run(
                                ["git", "diff", "--name-only", "HEAD"],
                                capture_output=True,
                                text=True,
                                timeout=5,
                            )
                            u = _sp.run(
                                ["git", "ls-files", "--others", "--exclude-standard"],
                                capture_output=True,
                                text=True,
                                timeout=5,
                            )
                            after = set(
                                (r.stdout.strip() + "\n" + u.stdout.strip())
                                .strip()
                                .splitlines(),
                            )
                            before = set(
                                before_files.splitlines() if before_files else [],
                            )
                            new_files = after - before
                            reverted: list[str] = []
                            for f in sorted(new_files):
                                if not f:
                                    continue
                                # Try git checkout first (tracked modified files)
                                cr = _sp.run(
                                    ["git", "checkout", "HEAD", "--", f],
                                    capture_output=True,
                                    timeout=5,
                                )
                                if cr.returncode != 0:
                                    # Untracked new file — remove it
                                    import os as _os

                                    try:
                                        _os.remove(f)
                                    except OSError:
                                        continue
                                reverted.append(f)
                            return reverted
                        except Exception:
                            return []

                    inner = user_input[1:].strip()  # strip *
                    if not inner:
                        _pe("Usage: *@command [args] or *$skill @command [args]")
                        continue

                    skill_names, cmd_name, remaining = ctx.parse_chained_input(inner)

                    if cmd_name is None:
                        _pe("Eval requires an @command (e.g., *@review file.py)")
                        continue

                    # No args → run eval suite
                    if not remaining and not skill_names:
                        suite = ctx.get_eval_suite(cmd_name)
                        if suite is None:
                            _pe(
                                f"No eval suite found for @{cmd_name}. Create {cmd_name}.eval.md next to the command.",
                            )
                            continue

                        _pi(
                            f"Running eval suite for @{cmd_name}: {len(suite.cases)} test case(s)",
                        )
                        total_pass = 0
                        total_criteria = 0

                        for case_idx, case in enumerate(suite.cases, 1):
                            _pi(f"\n── Case {case_idx}/{len(suite.cases)}: {case.name}")

                            # Snapshot git state before the command runs
                            _pre_files = _snapshot_git()

                            # Build the chain input
                            chain_blocks: list[str] = []
                            for sname in case.skills:
                                body = ctx.resolve_dollar_skill(sname)
                                if body:
                                    chain_blocks.append(body)

                            resolved = ctx.resolve_at_command(cmd_name, case.input_args)
                            if resolved is None:
                                _pe(f"  Failed to resolve @{cmd_name}")
                                continue
                            chain_blocks.append(resolved.body)

                            # Inject preferred-tools hint if specified
                            if case.preferred_tools:
                                tools_hint = (
                                    "Preferred tools for this task: "
                                    + ", ".join(case.preferred_tools)
                                )
                                chain_blocks.append(tools_hint)

                            chain_input = "\n\n---\n\n".join(chain_blocks)

                            # Enable tools if command allows them
                            eval_kwargs = dict(loop_kwargs)
                            if resolved.meta.tools_enabled:
                                eval_kwargs.pop("tool_choice", None)

                            # Run the command
                            for run in range(suite.runs_per_case):
                                if suite.runs_per_case > 1:
                                    _pi(f"  Run {run + 1}/{suite.runs_per_case}")
                                response = await send_message(
                                    ctx,
                                    chain_input,
                                    eval_kwargs,
                                    streaming_status=ss,
                                )
                                ss.reset()

                                # Grade the response
                                grading = ctx.build_grading_prompt(
                                    cmd_name,
                                    case.input_args,
                                    response,
                                    case.criteria,
                                )
                                _pi("  Grading...")
                                grade_response = await send_message(
                                    ctx,
                                    grading,
                                    loop_kwargs,
                                    streaming_status=ss,
                                )
                                ss.reset()

                                total_criteria += len(case.criteria)
                                # Count PASSes in the grade response
                                pass_count = grade_response.upper().count("| PASS")
                                total_pass += pass_count

                                # Revert file changes if eval did not pass all criteria
                                if pass_count < len(case.criteria):
                                    reverted = _revert_changes(_pre_files)
                                    if reverted:
                                        _pe(
                                            f"  Eval failed ({pass_count}/{len(case.criteria)}) "
                                            f"— reverted {len(reverted)} file(s): "
                                            + ", ".join(reverted),
                                        )
                                    else:
                                        _pe(
                                            f"  Eval failed ({pass_count}/{len(case.criteria)}) — no file changes to revert",
                                        )

                        _pi(
                            f"\n── Eval complete: {total_pass}/{total_criteria} criteria passed",
                        )
                        continue

                    # Has args → single run + grade
                    # Resolve the chain
                    blocks: list[str] = []
                    _abort = False
                    for sname in skill_names:
                        body = ctx.resolve_dollar_skill(sname)
                        if body is None:
                            _pe(f"Unknown skill: ${sname}")
                            _abort = True
                            break
                        blocks.append(body)
                    if _abort:
                        continue

                    resolved = ctx.resolve_at_command(cmd_name, remaining)
                    if resolved is None:
                        _pe(f"Unknown command: @{cmd_name}")
                        continue
                    blocks.append(resolved.body)
                    chain_input = "\n\n---\n\n".join(blocks)

                    # Enable tools if command allows them
                    eval_kwargs = dict(loop_kwargs)
                    if resolved.meta.tools_enabled:
                        eval_kwargs.pop("tool_choice", None)

                    _pi(f"*@{cmd_name}: running + grading")

                    # Snapshot git state before the command runs
                    _pre_files = _snapshot_git()

                    # Run
                    response = await send_message(
                        ctx,
                        chain_input,
                        eval_kwargs,
                        streaming_status=ss,
                    )
                    ss.reset()

                    # Use command-specific eval criteria when declared in
                    # frontmatter, falling back to generic 5-criteria grading.
                    cmd_criteria = getattr(resolved.meta, "eval_criteria", None)
                    criteria = cmd_criteria or [
                        "Response is relevant to the command's purpose",
                        "Response follows the command's output format",
                        "Response is complete (not truncated or missing sections)",
                        "Response is accurate (no hallucinated information)",
                        "Response is actionable (provides specific, useful details)",
                    ]
                    pass_threshold = getattr(
                        resolved.meta,
                        "eval_pass_threshold",
                        None,
                    ) or len(criteria)
                    grading = ctx.build_grading_prompt(
                        cmd_name,
                        remaining,
                        response,
                        criteria,
                    )
                    _pi("Grading...")
                    grade_response = await send_message(
                        ctx,
                        grading,
                        loop_kwargs,
                        streaming_status=ss,
                    )
                    ss.reset()

                    # Persist eval result to the eval store
                    try:
                        import time as _time

                        from obscura.eval.models import EvalRunSummary
                        from obscura.eval.store import EvalResultStore

                        _pass_ct = grade_response.upper().count("| PASS")
                        _fail_ct = len(criteria) - _pass_ct
                        summary = EvalRunSummary(
                            run_id=f"cmd-{cmd_name}-{int(_time.time())}",
                            suite_id=f"command:{cmd_name}",
                            backend=str(getattr(ctx, "backend_name", "unknown")),
                            model=str(getattr(ctx, "model_name", "unknown")),
                            total_cases=1,
                            passed=1 if _pass_ct >= pass_threshold else 0,
                            failed=0 if _pass_ct >= pass_threshold else 1,
                            regressions=0,
                            errors=0,
                            avg_deterministic_score=_pass_ct / max(len(criteria), 1),
                            avg_judge_score=None,
                            avg_composite_score=_pass_ct / max(len(criteria), 1),
                        )
                        store = EvalResultStore()
                        import asyncio as _aio

                        _aio.create_task(store.save_run(summary))
                    except Exception:
                        pass  # eval store not available — non-fatal

                    # Revert file changes if eval did not meet threshold
                    pass_count = grade_response.upper().count("| PASS")
                    total = len(criteria)
                    _pi(
                        f"Score: {pass_count}/{total} (threshold: {pass_threshold}/{total})",
                    )
                    if pass_count < pass_threshold:
                        reverted = _revert_changes(_pre_files)
                        if reverted:
                            _pe(
                                f"Eval failed ({pass_count}/{total}) "
                                f"— reverted {len(reverted)} file(s): "
                                + ", ".join(reverted),
                            )
                        else:
                            _pe(
                                f"Eval failed ({pass_count}/{total}) — no file changes to revert",
                            )
                    else:
                        _pi(f"Eval passed ({pass_count}/{total}) — changes kept")
                    continue

                # $skill / @command / chained input
                if user_input.startswith(("$", "@")):
                    from obscura.cli.render import print_error as _pe
                    from obscura.cli.render import print_info as _pi

                    skill_names, cmd_name, remaining = ctx.parse_chained_input(
                        user_input,
                    )
                    blocks: list[str] = []
                    _abort = False

                    # Resolve $skills as context
                    for sname in skill_names:
                        body = ctx.resolve_dollar_skill(sname)
                        if body is None:
                            _pe(
                                f"Unknown skill: ${sname}. Available: {', '.join(ctx.discover_dollar_skills())}",
                            )
                            _abort = True
                            break
                        _pi(f"${sname}")
                        blocks.append(body)

                    if _abort:
                        continue

                    # Resolve @command with args
                    _cmd_allowed_tools = False
                    if cmd_name is not None:
                        resolved = ctx.resolve_at_command(cmd_name, remaining)
                        if resolved is None:
                            _pe(
                                f"Unknown command: @{cmd_name}. Available: {', '.join(ctx.discover_at_commands())}",
                            )
                            continue
                        _pi(f"@{resolved.name}: {resolved.description}")
                        blocks.append(resolved.body)
                        if resolved.meta.tools_enabled:
                            _cmd_allowed_tools = True
                    elif remaining:
                        blocks.append(remaining)

                    # Compose final input and fall through to send_message
                    user_input = "\n\n---\n\n".join(blocks)

                    # If command allows tools, ensure they're enabled
                    if _cmd_allowed_tools:
                        loop_kwargs.pop("tool_choice", None)

                # Rebuild loop_kwargs in case tools were toggled
                if not ctx.tools_enabled and "tool_choice" not in loop_kwargs:
                    loop_kwargs["tool_choice"] = ToolChoice.none()
                elif ctx.tools_enabled:
                    loop_kwargs.pop("tool_choice", None)

                # Tips: record user message and maybe show a tip.
                if _tip_scheduler is not None:
                    _tip_scheduler.record_message()
                    tip = _tip_scheduler.get_tip()
                    if tip:
                        console.print(f"[dim italic]{tip}[/]")

                # Chat message: run in background so prompt stays responsive.
                # The StreamingStatus drives the toolbar spinner instead of
                # console.status() which conflicts with patch_stdout.
                task = asyncio.create_task(
                    send_message(ctx, user_input, loop_kwargs, streaming_status=ss),
                )
                background_tasks.add(task)

                def _on_done(t: asyncio.Task[str]) -> None:
                    background_tasks.discard(t)
                    ss.reset()

                task.add_done_callback(_on_done)

        finally:
            spinner_task.cancel()
            # Stop supervisor fleet
            if supervisor_task is not None:
                if _supervisor is not None:
                    with contextlib.suppress(Exception):
                        await _supervisor.stop()
                if not supervisor_task.done():
                    supervisor_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await supervisor_task
            if daemon_task is not None:
                if not daemon_task.done():
                    daemon_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await daemon_task
                # Close the daemon's dedicated client
                dc = getattr(daemon_task, "_daemon_client", None)
                if dc is not None:
                    with contextlib.suppress(Exception):
                        await dc.__aexit__(None, None, None)
            if background_tasks:
                await asyncio.gather(*background_tasks, return_exceptions=True)
            await ctx.stop_runtime()
            # Stop UDS inbox.
            if _uds_inbox is not None:
                with contextlib.suppress(Exception):
                    await _uds_inbox.stop()
            # Flush deep log.
            try:
                from obscura.core.deep_log import dlog

                dlog.session_event("end", session_id=ctx.session_id)
                dlog.flush()
                dlog.close()
            except Exception:
                pass
            # KAIROS: stop engine if not already handled by supervisor hooks
            if _kairos_engine is not None and not _kairos_hooks_registered:
                with contextlib.suppress(Exception):
                    await _kairos_engine.stop()
            # Run cleanup tasks (stale files, etc.)
            try:
                from obscura.core.cleanup import run_cleanup

                await run_cleanup()
            except Exception:
                pass
            # Save commit attribution
            try:
                from obscura.core.commit_attribution import get_attribution_tracker

                get_attribution_tracker().save()
            except Exception:
                pass
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


@click.group(invoke_without_command=True)
@click.argument("prompt", required=False, default=None)
@click.option(
    "-b",
    "--backend",
    default="copilot",
    type=click.Choice([b.value for b in Backend]),
    help="Backend to use.",
)
@click.option("-m", "--model", default=None, help="Model ID override.")
@click.option("-s", "--system", default="", help="System prompt.")
@click.option("--session", default=None, help="Resume session by ID.")
@click.option(
    "--continue",
    "resume_last",
    is_flag=True,
    default=False,
    help="Resume the most recent session.",
)
@click.option(
    "--resume",
    default=None,
    help="Resume session by ID (alias for --session).",
)
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
@click.option(
    "-w",
    "--workspace",
    "workspace_name",
    default=None,
    help="Load a workspace from .obscura/specs/ (e.g. 'code-mode').",
)
@click.option(
    "--log-level",
    "log_level",
    default="WARNING",
    type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"], case_sensitive=False),
    help="Console log level.",
)
@click.option(
    "--supervise/--no-supervise",
    default=True,
    help="Launch the agent fleet from agents.yaml (default: on).",
)
@click.pass_context
def main(
    ctx: click.Context,
    prompt: str | None,
    backend: str,
    model: str | None,
    system: str,
    session: str | None,
    resume_last: bool,
    resume: str | None,
    max_turns: int,
    tools: str,
    confirm: bool,
    no_default_prompt: bool,
    workspace_name: str | None,
    log_level: str,
    supervise: bool,
) -> None:
    """Obscura — AI agent REPL."""
    # If a subcommand was invoked, let Click handle it
    if ctx.invoked_subcommand is not None:
        return

    import logging as _logging

    # configure_logger intentionally imported lazily when needed

    cli_logger = _logging.getLogger("obscura")
    # Set the InfoHandler threshold to the user's chosen level
    level = getattr(_logging, log_level.upper(), _logging.WARNING)
    for h in cli_logger.handlers:
        if h.__class__.__name__ == "InfoHandler":
            h.setLevel(level)

    # Sync OBSCURA.md ↔ CLAUDE.md before anything else touches the workspace.
    _sync_guide_files()

    # Disable provider permission layers — Obscura's policy engine is authoritative.
    _sync_provider_settings()

    # Compile workspace if specified
    compiled_ws = None
    if workspace_name is not None:
        try:
            from obscura.core.compiler.compile import compile_workspace

            compiled_ws = compile_workspace(workspace_name, strict=False)
            # Apply workspace config to CLI defaults
            ws_backend = compiled_ws.config.get("default_backend")
            if ws_backend and isinstance(ws_backend, str):
                backend = ws_backend
            click.echo(
                f"Loaded workspace '{compiled_ws.name}' "
                f"({len(compiled_ws.agents)} agents, "
                f"{len(compiled_ws.policies)} policies)",
            )
        except Exception as exc:
            click.echo(f"Failed to load workspace '{workspace_name}': {exc}", err=True)

    # Resolve session ID: --resume > --session > --continue (last session)
    resolved_session = resume or session
    if not resolved_session and resume_last:
        try:
            import sqlite3

            db_path = resolve_obscura_home() / "events.db"
            con = sqlite3.connect(str(db_path))
            row = con.execute(
                "SELECT id FROM sessions ORDER BY updated_at DESC LIMIT 1",
            ).fetchone()
            con.close()
            if row:
                resolved_session = row[0]
        except Exception:
            pass
    try:
        asyncio.run(
            _repl(
                backend,
                model,
                system,
                resolved_session,
                max_turns,
                tools,
                prompt,
                confirm,
                no_default_prompt,
                supervise=supervise,
                compiled_ws=compiled_ws,
            ),
        )
    except KeyboardInterrupt:
        pass  # graceful exit on Ctrl-C


@main.command()
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Reinitialise even if .obscura/ exists.",
)
@click.option(
    "--no-bootstrap",
    is_flag=True,
    default=False,
    help="Skip plugin dependency bootstrapping.",
)
def init(force: bool, no_bootstrap: bool) -> None:
    """Initialise a local .obscura/ workspace and bootstrap plugin deps."""
    from obscura.core.workspace import (
        WorkspaceExistsError,
        bootstrap_all_builtins,
        init_workspace,
    )

    # Sync OBSCURA.md ↔ CLAUDE.md as part of workspace init.
    _sync_guide_files()
    _sync_provider_settings()

    try:
        ws = init_workspace(force=force)
        click.echo(f"Workspace initialised at {ws}")
    except WorkspaceExistsError:
        click.echo(".obscura/ already exists. Use --force to reinitialise.")
        if no_bootstrap:
            return
    except Exception as exc:
        click.echo(f"Init failed: {exc}", err=True)
        return

    if not no_bootstrap:
        click.echo("Bootstrapping plugin dependencies...")
        try:
            summary = bootstrap_all_builtins()
            if summary["installed"]:
                click.echo(f"  Installed: {', '.join(summary['installed'])}")
            if summary["skipped"]:
                click.echo(f"  Already present: {len(summary['skipped'])} deps")
            if summary["errors"]:
                click.echo(f"  Failed: {', '.join(summary['errors'])}", err=True)
            if summary["warnings"]:
                for w in summary["warnings"]:
                    click.echo(f"  Warning: {w}", err=True)
            if not summary["errors"]:
                click.echo("All plugin dependencies bootstrapped.")
            else:
                click.echo(
                    "Some deps failed. Install manually: "
                    + ", ".join(e.split(":")[0] for e in summary["errors"]),
                )
        except Exception as exc:
            click.echo(f"Bootstrap failed: {exc}", err=True)


# ---------------------------------------------------------------------------
# Workspace subcommands
# ---------------------------------------------------------------------------


@main.group()
def workspace() -> None:
    """Manage workspaces (list, inspect, compile)."""


@workspace.command("list")
def workspace_list() -> None:
    """List available workspaces from specs directory."""
    from obscura.core.compiler.loader import load_specs_dir

    specs_dir = resolve_obscura_specs_dir()
    if not specs_dir.is_dir():
        click.echo(f"No specs directory at {specs_dir}")
        return

    registry = load_specs_dir(specs_dir)
    if not registry.workspaces:
        click.echo("No workspaces found.")
        return

    for name, ws in sorted(registry.workspaces.items()):
        desc = ws.metadata.description or "(no description)"
        n_agents = len(ws.spec.agents)
        n_policies = len(ws.spec.policies)
        click.echo(f"  {name:20s}  {n_agents} agents, {n_policies} policies  {desc}")


@workspace.command("inspect")
@click.argument("name")
def workspace_inspect(name: str) -> None:
    """Compile and inspect a workspace."""
    from obscura.core.compiler.compile import compile_workspace_from_dir
    from obscura.core.compiler.errors import CompileError

    specs_dir = resolve_obscura_specs_dir()
    try:
        ws = compile_workspace_from_dir(name, specs_dir, strict=False)
    except CompileError as exc:
        click.echo(f"Compile error: {exc}", err=True)
        return

    click.echo(f"Workspace: {ws.name}")
    click.echo(f"  Config: {ws.config or '(empty)'}")
    click.echo(f"  Preload plugins: {ws.preload_plugins}")

    if ws.policies:
        click.echo(f"  Policies: {', '.join(p.name for p in ws.policies)}")
    if ws.plugin_include:
        click.echo(f"  Plugin include: {', '.join(sorted(ws.plugin_include))}")
    if ws.plugin_exclude:
        click.echo(f"  Plugin exclude: {', '.join(sorted(ws.plugin_exclude))}")
    if ws.memory:
        click.echo(
            f"  Memory: namespace={ws.memory.namespace} scope={ws.memory.shared_scope}",
        )

    if ws.agents:
        click.echo(f"  Agents ({len(ws.agents)}):")
        for a in ws.agents:
            click.echo(
                f"    {a.name:20s}  template={a.template_name}  "
                f"mode={a.mode}  provider={a.provider}  "
                f"plugins=[{', '.join(a.plugins)}]",
            )

    if ws.startup_agents:
        click.echo(f"  Startup: {', '.join(ws.startup_agents)}")


@workspace.command("load")
@click.argument("name")
def workspace_load(name: str) -> None:
    """Compile a workspace and display its configuration for the session."""
    from obscura.core.compiler.compile import compile_workspace
    from obscura.core.compiler.errors import CompileError

    try:
        ws = compile_workspace(name, strict=False)
    except CompileError as exc:
        click.echo(f"Compile error: {exc}", err=True)
        return

    click.echo(f"Loaded workspace: {ws.name}")
    if ws.agents:
        click.echo(f"  Agents: {', '.join(a.name for a in ws.agents)}")
    if ws.policies:
        click.echo(f"  Policies: {', '.join(p.name for p in ws.policies)}")
    if ws.plugin_include:
        click.echo(f"  Allowed plugins: {', '.join(sorted(ws.plugin_include))}")
    if ws.plugin_exclude:
        click.echo(f"  Blocked plugins: {', '.join(sorted(ws.plugin_exclude))}")
    if ws.startup_agents:
        click.echo(f"  Startup agents: {', '.join(ws.startup_agents)}")
    click.echo(f"  Preload plugins: {ws.preload_plugins}")
    click.echo("Workspace compiled successfully. Use -w flag to apply at startup.")


# ---------------------------------------------------------------------------
# Template subcommands
# ---------------------------------------------------------------------------


@main.group()
def template() -> None:
    """Manage templates (list, inspect)."""


@template.command("list")
def template_list() -> None:
    """List available templates from specs directory."""
    from obscura.core.compiler.loader import load_specs_dir

    specs_dir = resolve_obscura_specs_dir()
    if not specs_dir.is_dir():
        click.echo(f"No specs directory at {specs_dir}")
        return

    registry = load_specs_dir(specs_dir)
    if not registry.templates:
        click.echo("No templates found.")
        return

    for name, tmpl in sorted(registry.templates.items()):
        extends = f"extends={tmpl.spec.extends}" if tmpl.spec.extends else ""
        plugins = ", ".join(tmpl.spec.plugins) if tmpl.spec.plugins else "(none)"
        click.echo(f"  {name:20s}  {extends:20s}  plugins=[{plugins}]")


@template.command("inspect")
@click.argument("name")
def template_inspect(name: str) -> None:
    """Inspect a template (with inheritance resolved)."""
    from obscura.core.compiler.errors import CompileError
    from obscura.core.compiler.loader import load_specs_dir
    from obscura.core.compiler.merger import merge_template_chain
    from obscura.core.compiler.resolver import resolve_template_chain

    specs_dir = resolve_obscura_specs_dir()
    registry = load_specs_dir(specs_dir)

    tmpl = registry.get_template(name)
    if tmpl is None:
        click.echo(f"Template '{name}' not found.", err=True)
        return

    try:
        chain = resolve_template_chain(tmpl, registry)
        merged = merge_template_chain(chain)
    except CompileError as exc:
        click.echo(f"Resolution error: {exc}", err=True)
        return

    spec = merged.spec
    click.echo(f"Template: {merged.metadata.name}")
    if merged.metadata.description:
        click.echo(f"  Description: {merged.metadata.description}")
    if merged.metadata.tags:
        click.echo(f"  Tags: {', '.join(merged.metadata.tags)}")
    click.echo(f"  Provider: {spec.provider}")
    if spec.model_id:
        click.echo(f"  Model: {spec.model_id}")
    click.echo(f"  Agent type: {spec.agent_type}")
    click.echo(f"  Max iterations: {spec.max_iterations}")
    if spec.plugins:
        click.echo(f"  Plugins: {', '.join(spec.plugins)}")
    if spec.capabilities:
        click.echo(f"  Capabilities: {', '.join(spec.capabilities)}")
    if spec.tool_allowlist is not None:
        click.echo(f"  Tool allowlist: {', '.join(spec.tool_allowlist)}")
    if spec.tool_denylist:
        click.echo(f"  Tool denylist: {', '.join(spec.tool_denylist)}")
    if spec.instructions:
        preview = spec.instructions[:200]
        if len(spec.instructions) > 200:
            preview += "..."
        click.echo(f"  Instructions: {preview}")


# ---------------------------------------------------------------------------
# Kairos goal runtime CLI — registered as `obscura kairos <subcommand>`
# ---------------------------------------------------------------------------

from obscura.cli.kairos_commands import kairos_group as _kairos_group

main.add_command(_kairos_group)


# Backwards-compat aliases added by test harness
def _emit_context_warnings(*args, **kwargs):
    from .warnings import emit_context_warnings as _impl

    return _impl(*args, **kwargs)


def _copilot_budget_pct(tokens: int, context_window: int):
    from .warnings import get_copilot_budget_pct as _impl

    return _impl(tokens, context_window)


def _parse_confirm_decision(answer: str) -> str | None:
    a = (answer or "").lower()
    if "approve" in a or a.strip().startswith("yes") or "accept" in a:
        return "approve"
    if "deny" in a or a.strip().startswith("no") or "do not" in a or "dont" in a:
        return "deny"
    return None


def _track_task_surface_event(ctx, ev) -> None:
    """Compatibility stub: track a task-surface event (no-op)."""
    return

# Lazy compatibility wrappers imported at module import end to avoid E402

# Lazy compatibility wrappers imported at module import end to avoid E402

# Lazy compatibility wrappers imported at module import end to avoid E402

# Lazy compatibility wrappers imported at module import end to avoid E402

# Lazy compatibility wrappers imported at module import end to avoid E402

# Lazy compatibility wrappers imported at module import end to avoid E402

# Lazy compatibility wrappers imported at module import end to avoid E402

# Lazy compatibility wrappers imported at module import end to avoid E402
from obscura.cli._compat import (
    _discover_mcp,
    _parse_inline_agent_mention,
    _run_inline_agent_from_mention,
    _cli_confirm,
)  # noqa: E402  # lazy wrappers

# Load extra CLI commands (caffeinate)
try:
    from . import commands_extra  # noqa: F401
except Exception:
    # Non-fatal: availability is best-effort
    pass

# Load extra CLI commands (caffeinate)
try:
    from . import commands_extra  # noqa: F401
except Exception:
    # Non-fatal: availability is best-effort
    pass

# Load extra CLI commands (caffeinate)
try:
    from . import commands_extra  # noqa: F401
except Exception:
    # Non-fatal: availability is best-effort
    pass

# Load extra CLI commands (caffeinate)
try:
    from . import commands_extra  # noqa: F401
except Exception:
    # Non-fatal: availability is best-effort
    pass

# Load extra CLI commands (caffeinate)
try:
    from . import commands_extra  # noqa: F401
except Exception:
    # Non-fatal: availability is best-effort
    pass

# Load extra CLI commands (caffeinate)
try:
    from . import commands_extra  # noqa: F401
except Exception:
    # Non-fatal: availability is best-effort
    pass

# Load extra CLI commands (caffeinate)
try:
    from . import commands_extra  # noqa: F401
except Exception:
    # Non-fatal: availability is best-effort
    pass

# Load extra CLI commands (caffeinate)
try:
    from . import commands_extra  # noqa: F401
except Exception:
    # Non-fatal: availability is best-effort
    pass
