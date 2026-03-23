"""obscura.cli.repl — REPL message dispatch and context-tracking helpers.

Extracted from cli/__init__.py (Fix 7: god-module decomposition).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

from obscura.cli.commands import (
    REPLContext,
    _FILE_WRITE_TOOLS,
    cmd_compact,
    estimate_effective_context_tokens,
)
from obscura.cli.render import StreamRenderer, console
from obscura.cli import trace as trace_mod
from obscura.cli.vector_memory_bridge import auto_save_turn, search_relevant_context
from obscura.core.types import AgentEventKind


async def _cli_confirm(ctx: REPLContext, tool_name: str, tool_input: dict[str, Any]) -> bool:
    if tool_name in ctx.confirm_always:
        return True
    from obscura.cli.widgets import ToolConfirmRequest, confirm_tool
    result = await confirm_tool(ToolConfirmRequest(tool_name=tool_name, tool_input=tool_input))
    if result.action == "always_allow":
        ctx.confirm_always.add(tool_name)
        return True
    return result.action == "allow"


def _track_file_event(event_kind: AgentEventKind, ctx: REPLContext, ev: Any) -> None:
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


def _maybe_parse_plan(response_text: str, ctx: REPLContext) -> None:
    mm = ctx._mode_manager
    if mm is None:
        return
    from obscura.cli.app.modes import TUIMode
    if mm.current != TUIMode.PLAN or not response_text.strip():
        return
    from obscura.cli.app.modes import Plan
    from obscura.cli.render import render_plan
    plan = Plan.parse(response_text)
    if plan.steps:
        mm.active_plan = plan
        render_plan(plan)


async def send_message(
    ctx: REPLContext,
    text: str,
    loop_kwargs: dict[str, Any],
    streaming_status: Any | None = None,
) -> str:
    """Send a chat message and stream the response. Returns accumulated assistant text."""
    from obscura.cli.bootstrap import _run_inline_agent_from_mention  # type: ignore[reportPrivateUsage]
    from obscura.cli.render import set_active_renderer
    from obscura.tools.system import update_token_usage

    inline_agent_response = await _run_inline_agent_from_mention(ctx, text)
    if inline_agent_response is not None:
        ctx.message_history.append(("user", text))
        if inline_agent_response:
            ctx.message_history.append(("assistant", inline_agent_response))
        if ctx.vector_store is not None and inline_agent_response:
            turn_num = len([m for m in ctx.message_history if m[0] == "user"])
            auto_save_turn(ctx.vector_store, ctx.session_id, text, inline_agent_response, turn_number=turn_num)
        return inline_agent_response

    renderer = StreamRenderer(streaming_status=streaming_status)
    try:
        set_active_renderer(renderer)
    except Exception:
        pass
    accumulated: list[str] = []

    from obscura.core.types import ToolCallInfo
    confirm_cb: Callable[[ToolCallInfo], Coroutine[Any, Any, bool]] | None = None
    if ctx.confirm_enabled:
        async def _confirm_cb_impl(tc: ToolCallInfo) -> bool:
            return await _cli_confirm(ctx, tc.name, tc.input)
        confirm_cb = _confirm_cb_impl

    _context_window = ctx.client.context_window
    _compact_threshold = int(_context_window * 0.60)
    _warn_threshold = ctx.client.context_warn_threshold

    augmented_text = text
    slash_skill_context = ctx.build_active_skill_context()
    if slash_skill_context:
        augmented_text = f"{slash_skill_context}\n\n---\n\n{augmented_text}"
    if ctx.vector_store is not None:
        vm_context = search_relevant_context(ctx.vector_store, text, top_k=3)
        if vm_context:
            augmented_text = f"{vm_context}\n\n---\n\n{augmented_text}"

    _pre_tokens = estimate_effective_context_tokens(ctx, pending_user_text=augmented_text)
    update_token_usage(input_tokens=_pre_tokens, context_window=_context_window, compact_threshold=_compact_threshold)
    _stream_output_chars = 0
    _stream_output_tokens_sent = 0
    _last_usage_push = 0.0

    def _push_stream_token_usage(force: bool = False) -> None:
        nonlocal _stream_output_tokens_sent, _last_usage_push
        est = _stream_output_chars // 4
        now = time.monotonic()
        if not force:
            if est - _stream_output_tokens_sent < 32:
                return
            if now - _last_usage_push < 0.75:
                return
        update_token_usage(input_tokens=_pre_tokens, output_tokens=est,
                           context_window=_context_window, compact_threshold=_compact_threshold)
        _stream_output_tokens_sent = est
        _last_usage_push = now

    if _pre_tokens > _compact_threshold:
        console.print(f"[yellow]⚡ Auto-compacting context (~{_pre_tokens:,} tokens, 60% of {_context_window:,}) …[/]")
        await cmd_compact("6", ctx)

    async def _stream_with_retry(context_retry_used: bool = False, dead_session_retry_used: bool = False) -> list[str]:
        nonlocal _stream_output_chars
        _buf: list[str] = []
        _s = ctx.client.run_loop(
            augmented_text, max_turns=ctx.max_turns, event_store=ctx.store,
            session_id=ctx.session_id, auto_complete=False, on_confirm=confirm_cb, **loop_kwargs,
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
                    tool_names = [event.tool_name] if getattr(event, "tool_name", None) else []
                    trace_mod.append_event(event.kind.name, preview=preview, tool_names=tool_names)
                except Exception:
                    pass
                if event.kind == AgentEventKind.TEXT_DELTA:
                    _buf.append(event.text)
                    _stream_output_chars += len(event.text)
                    _push_stream_token_usage()
                _track_file_event(event.kind, ctx, event)
        except KeyboardInterrupt:
            pass
        except Exception as exc:
            _err = str(exc).lower()
            _ctx = any(kw in _err for kw in ("prompt is too long", "context window", "too many tokens", "maximum context length", "request too large"))
            _dead = any(kw in _err for kw in ("dead process", "cannot send message", "can't send message", "cannot write to terminated", "write to terminated process", "terminated process", "exit code", "session is closed", "session closed"))
            if _ctx and not context_retry_used:
                console.print("[red]⚠ Context limit reached — aggressive compact and retry…[/]")
                await cmd_compact("2", ctx)
                return await _stream_with_retry(context_retry_used=True, dead_session_retry_used=dead_session_retry_used)
            if _dead and not dead_session_retry_used:
                console.print("[yellow]⚠ Backend session became stale — recreating and retrying once…[/]")
                try:
                    await ctx.client.reset_session()
                except Exception:
                    try:
                        await ctx.recreate_client(ctx.backend, ctx.model)
                    except Exception:
                        pass
                return await _stream_with_retry(context_retry_used=context_retry_used, dead_session_retry_used=True)
            raise
        return _buf

    try:
        accumulated = await _stream_with_retry()
    except KeyboardInterrupt:
        pass
    finally:
        renderer.finish()
        try:
            set_active_renderer(None)
        except Exception:
            pass

    console.print()
    response_text = "".join(accumulated)
    ctx.message_history.append(("user", text))
    if response_text:
        ctx.message_history.append(("assistant", response_text))
    if ctx.vector_store is not None and response_text:
        turn_num = len([m for m in ctx.message_history if m[0] == "user"])
        auto_save_turn(ctx.vector_store, ctx.session_id, text, response_text, turn_number=turn_num)

    _push_stream_token_usage(force=True)
    _post_tokens = estimate_effective_context_tokens(ctx)
    update_token_usage(input_tokens=_post_tokens, output_tokens=len(response_text) // 4,
                       context_window=_context_window, compact_threshold=_compact_threshold)
    if _warn_threshold < _post_tokens <= _compact_threshold:
        console.print(f"[dim yellow]  Context: ~{_post_tokens:,} tokens ({int(_post_tokens/_context_window*100)}% of {_context_window:,}). Auto-compact at {_compact_threshold:,} (60%).[/]")

    _maybe_parse_plan(response_text, ctx)

    # Skip auto-detection if ask_user tool already presented a widget this turn.
    try:
        from obscura.tools.system import was_ask_user_called, reset_ask_user_called

        _tool_asked = was_ask_user_called()
        reset_ask_user_called()

        if not _tool_asked:
            from obscura.cli.widgets import detect_question_choices, present_detected_choices
            detected = detect_question_choices(response_text)
            if detected is not None:
                selection = await present_detected_choices(detected)
                if selection is not None:
                    return await send_message(ctx, selection, loop_kwargs, streaming_status)
    except Exception:
        pass

    return response_text
