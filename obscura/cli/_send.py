"""obscura.cli._send — Chat message dispatch with streaming + retry.

Extracted from ``obscura/cli/__init__.py``.

Public API
----------
send_message(ctx, text, loop_kwargs, streaming_status=None) -> str
    Send a chat message and stream the response with Markdown rendering.
    Returns the accumulated assistant text.
"""

from __future__ import annotations

import contextlib
import logging
import time
from typing import TYPE_CHECKING, Any, cast

from obscura.cli import trace as trace_mod
from obscura.cli._tool_confirm import cli_confirm, maybe_parse_plan, track_file_event
from obscura.cli.bootstrap import (
    _run_inline_agent_from_mention,  # pyright: ignore[reportPrivateUsage]
)
from obscura.cli.commands import cmd_compact, estimate_effective_context_tokens
from obscura.cli.render import console, print_warning, set_active_renderer
from obscura.cli.renderer import create_renderer
from obscura.cli.tool_collapse import ToolCollapser
from obscura.cli.vector_memory_bridge import (
    auto_save_turn,
    search_relevant_context,
    search_with_router,
)
from obscura.cli.widgets import detect_question_choices, present_detected_choices
from obscura.core.compaction import should_auto_compact
from obscura.core.cost_tracker import get_cost_tracker
from obscura.core.deep_log import dlog
from obscura.core.permission_modes import PermissionMode, PermissionModeEngine
from obscura.core.session_utils import generate_session_title
from obscura.core.types import (
    EFFORT_THINKING_BUDGETS,
    AgentEventKind,
    EffortLevel,
    ToolCallInfo,
)
from obscura.tools.system import UI, Session

if TYPE_CHECKING:
    from obscura.cli.commands import REPLContext
    from obscura.cli.prompt import StreamingStatus

_log = logging.getLogger("obscura.cli")

# Mutable container tracked per-session so auto-title fires only once.
# Initialised/reset in _repl_loop before each interactive session.
_session_state: dict[str, bool] = {"titled": False}


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
                classifier=ctx.turn_classifier,
            )
        return inline_agent_response

    renderer = create_renderer(streaming_status=streaming_status)
    # Feed session context into the modern renderer's status bar
    _set_session_context = getattr(renderer, "set_session_context", None)
    if callable(_set_session_context):
        _ps = getattr(ctx, "_prompt_status", None)
        _set_session_context(
            title=getattr(_ps, "session_title", "") or "",
            model=ctx.model or "",
            ctx_pct=getattr(_ps, "ctx_pct", 0),
        )
    # Register active renderer so prompt can expand previews while streaming
    try:
        set_active_renderer(renderer)
    except Exception:
        _log.debug("suppressed exception in send_message", exc_info=True)
    accumulated: list[str] = []

    # Build confirm callback with permission mode integration.
    async def confirm_cb(tc: ToolCallInfo) -> bool:
        # 1. Check permission mode (dangerous patterns + mode restrictions).
        try:
            mode_str = getattr(ctx, "permission_mode", "default")
            engine = PermissionModeEngine(PermissionMode(mode_str))
            decision = engine.evaluate(tc.name, tc.input)
            if not decision.allowed:
                print_warning(f"Blocked by {mode_str} mode: {tc.name}")
                return False
            if decision.auto_approved:
                return True
        except Exception:
            _log.debug("suppressed exception in confirm_cb", exc_info=True)
        # 2. If confirm is enabled, prompt user.
        if ctx.confirm_enabled:
            return await cli_confirm(ctx, tc.name, tc.input)
        return True

    # ── Token-aware auto-compact ────────────────────────────────────────────
    _context_window = ctx.client.context_window
    _compact_threshold = int(_context_window * 0.60)  # compact at 60%
    _warn_threshold = ctx.client.context_warn_threshold  # 50% of window

    # ── Vector memory pre-search ──────────────────────────────────────────
    augmented_text = text
    slash_skill_context = ctx.build_active_skill_context()
    if slash_skill_context:
        augmented_text = f"{slash_skill_context}\n\n---\n\n{augmented_text}"

    if ctx.vector_store is not None:
        if ctx.context_router is not None:
            vm_context = search_with_router(ctx.context_router, text)
        else:
            vm_context = search_relevant_context(ctx.vector_store, text, top_k=3)
        if vm_context:
            augmented_text = f"{vm_context}\n\n---\n\n{augmented_text}"

    _pre_tokens = estimate_effective_context_tokens(
        ctx,
        pending_user_text=augmented_text,
    )
    Session.update_token_usage(
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
        Session.update_token_usage(
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
        if hasattr(ctx, "effort_level") and ctx.effort_level:
            try:
                _lvl = EffortLevel(ctx.effort_level)
                _effective_kwargs["max_thinking_tokens"] = EFFORT_THINKING_BUDGETS[_lvl]
            except (ValueError, KeyError):
                _log.debug("suppressed exception in _stream_with_retry", exc_info=True)
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
                    _log.debug(
                        "suppressed exception in _stream_with_retry", exc_info=True
                    )
                if event.kind == AgentEventKind.TEXT_DELTA:
                    _buf.append(event.text)
                    _stream_output_chars += len(event.text)
                    _push_stream_token_usage()
                track_file_event(event.kind, ctx, event)
                # Deep logging
                try:
                    if event.kind == AgentEventKind.TOOL_CALL:
                        dlog.tool_call(
                            getattr(event, "tool_name", ""),
                            getattr(event, "tool_input", {}),
                        )
                    elif event.kind == AgentEventKind.TOOL_RESULT:
                        dlog.tool_call(
                            getattr(event, "tool_name", ""),
                            getattr(event, "tool_input", {}),
                            ok=not getattr(event, "is_error", False),
                            result_preview=str(getattr(event, "tool_result", ""))[:200],
                        )
                except Exception:
                    _log.debug(
                        "suppressed exception in _stream_with_retry", exc_info=True
                    )
                # Tool output collapsing
                if event.kind == AgentEventKind.TOOL_CALL:
                    tool_name = getattr(event, "tool_name", "")
                    tool_input = getattr(event, "tool_input", {})
                    try:
                        if not hasattr(ctx, "collapser"):
                            ctx.collapser = ToolCollapser()
                        ctx.collapser.record(tool_name, tool_input)
                    except Exception:
                        _log.debug(
                            "suppressed exception in _stream_with_retry", exc_info=True
                        )
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
                    collapser = getattr(ctx, "collapser", None)
                    if collapser is not None and collapser.pending:
                        try:
                            summary = collapser.flush_summary()
                            if summary:
                                console.print(f"[dim]  {summary}[/]")
                        except Exception:
                            _log.debug(
                                "suppressed exception in _stream_with_retry",
                                exc_info=True,
                            )
                # Track costs on turn completion.
                if event.kind in (
                    AgentEventKind.TURN_COMPLETE,
                    AgentEventKind.AGENT_DONE,
                ):
                    meta = getattr(event, "metadata", None)
                    if meta is not None:
                        _usage_raw: Any = getattr(meta, "usage", None) or {}
                        inp: int = 0
                        out: int = 0
                        if isinstance(_usage_raw, dict):
                            _usage_dict = cast(dict[str, Any], _usage_raw)
                            inp = int(_usage_dict.get("input_tokens", 0) or 0)
                            out = int(_usage_dict.get("output_tokens", 0) or 0)
                        else:
                            inp = int(getattr(_usage_raw, "input_tokens", 0) or 0)
                            out = int(getattr(_usage_raw, "output_tokens", 0) or 0)
                        if inp > 0 or out > 0:
                            try:
                                get_cost_tracker().record(
                                    inp,
                                    out,
                                    ctx.model or ctx.backend,
                                )
                            except Exception:
                                _log.debug(
                                    "suppressed exception in _stream_with_retry",
                                    exc_info=True,
                                )
        except KeyboardInterrupt:
            _log.debug("suppressed exception in _stream_with_retry", exc_info=True)
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
                    _log.debug(
                        "suppressed exception in _stream_with_retry", exc_info=True
                    )
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
        _log.debug("suppressed exception in send_message", exc_info=True)
    finally:
        renderer.finish()
        try:
            set_active_renderer(None)
        except Exception:
            _log.debug("suppressed exception in send_message", exc_info=True)

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
            classifier=ctx.turn_classifier,
        )

    # Post-send: update token tracker
    _push_stream_token_usage(force=True)
    _post_tokens = estimate_effective_context_tokens(ctx)
    Session.update_token_usage(
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

    # Auto-compact: trigger if context exceeds threshold.
    if _post_tokens > _compact_threshold:
        try:
            if should_auto_compact(
                [{"role": r, "content": t} for r, t in ctx.message_history],
                ctx.model or "default",
                system_prompt=ctx.system_prompt,
            ):
                console.print("[dim cyan]  Auto-compacting context...[/]")
                await cmd_compact("4", ctx)
        except Exception:
            _log.debug("suppressed exception in send_message", exc_info=True)

    # Auto-title: generate session title after first exchange.
    if not _session_state["titled"] and len(ctx.message_history) >= 2:
        _session_state["titled"] = True
        try:
            title = await generate_session_title(text, ctx.client._backend)  # pyright: ignore[reportPrivateUsage]
            if title:
                await ctx.store.update_session(ctx.session_id, summary=title)
                _prompt_status = getattr(ctx, "_prompt_status", None)
                if _prompt_status is not None:
                    _prompt_status.session_title = title
                console.print(
                    f"  [dim]session titled:[/] [bold bright_cyan]{title}[/]",
                    highlight=False,
                )
        except Exception:
            _log.debug("suppressed exception in send_message", exc_info=True)

    # Parse plan if in PLAN mode

    maybe_parse_plan(response_text, ctx)

    # Auto-detect question choices and present interactive widget.
    try:
        _tool_asked = UI.was_ask_user_called()
        UI.reset_ask_user_called()

        if not _tool_asked:
            detected = detect_question_choices(response_text)
            if detected is not None:
                selection = await present_detected_choices(detected)
                if selection is not None:
                    return await send_message(
                        ctx,
                        selection,
                        loop_kwargs,
                        streaming_status,
                    )
    except Exception:
        _log.debug("suppressed exception in send_message", exc_info=True)

    return response_text
