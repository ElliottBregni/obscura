"""obscura.cli.session — Reusable session orchestrator (internal API).

This module provides ``ObscuraSession``, a high-level session that wraps
:class:`~obscura.core.client.ObscuraClient` with tool assembly, vector memory,
KAIROS, hooks, and all the pre/post-processing that ``send_message`` needs.

The CLI (``obscura/cli/__init__.py``) creates an ``ObscuraSession`` and drives
it from its interactive REPL loop.  Scripts, tests, and the server can use
the same class without importing any CLI code.

Usage::

    config = SessionConfig(backend="claude", model="claude-sonnet-4-5-20250929")
    session = await ObscuraSession.create(config)
    response = await session.send("explain this code")
    await session.close()
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from obscura.cli.bootstrap import (
    _discover_mcp,
    _run_inline_agent_from_mention,
)
from obscura.cli.commands import (
    _FILE_WRITE_TOOLS,
    REPLContext,
    handle_command,
)
from obscura.cli.render import (
    console,
    print_warning,
    render_plan,
)
from obscura.cli.vector_memory_bridge import (
    auto_save_turn,
    init_vector_store,
    load_startup_memories,
    run_startup_maintenance,
    search_relevant_context,
    search_with_router,
)
from obscura.core.client import ObscuraClient
from obscura.core.event_store import SessionStatus, SQLiteEventStore
from obscura.core.paths import resolve_obscura_home
from obscura.core.types import (
    AgentEventKind,
    Backend,
    SessionRef,
    ToolChoice,
)

_log = logging.getLogger("obscura.cli.session")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_session_state: dict[str, bool] = {"titled": False}


def _swallow(label: str, exc: Exception) -> None:
    """Log a swallowed exception at DEBUG level instead of silently ignoring."""
    _log.debug("%s: %s: %s", label, type(exc).__name__, exc)


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


def _track_task_surface_event(ctx: Any, ev: Any) -> None:
    """Compatibility stub: track a task-surface event (no-op)."""
    return


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class SessionConfig:
    """All inputs needed to create an :class:`ObscuraSession`.

    Pure data — no CLI types, no Click context.
    """

    backend: str = "copilot"
    model: str | None = None
    system_prompt: str = ""
    session_id: str | None = None
    max_turns: int = 10
    tools_enabled: bool = True
    confirm: bool = False
    no_default_prompt: bool = False
    supervise: bool = True
    compiled_ws: Any | None = None
    # Caller-supplied MCP servers that should be added to whatever the
    # session discovers from ``~/.obscura/mcp/``.  Used by the browser
    # extension's native host to inject an ephemeral in-process MCP
    # server exposing the browser tools on Codex sessions.  Safe to
    # leave as ``None`` — equivalent to an empty list.
    extra_mcp_servers: list[dict[str, Any]] | None = None
    # Optional storage scope. When set, ``events.db`` and the SQLite
    # vector-memory directory are routed under
    # ``~/.obscura/profiles/<profile_id>/`` so multiple Chrome profiles
    # can run obscura side-by-side without sharing SQLite session ids.
    # ``None`` (the default) preserves legacy single-tenant behaviour:
    # the terminal REPL keeps writing to ``~/.obscura/events.db``.
    profile_id: str | None = None


def _resolve_profile_home(profile_id: str | None) -> Path:
    """Return the storage root for a given profile.

    With ``profile_id=None`` this is the legacy ``~/.obscura`` (terminal
    REPL behaviour, unchanged). With a profile id set, it's
    ``~/.obscura/profiles/<profile_id>``. The directory is *not* created
    here — callers materialise it on first write (``SQLiteEventStore``
    auto-creates parents, the vector memory backend auto-creates its dir).

    Note: this is *not* a migration helper. If the legacy ``events.db``
    exists and a profile-scoped one does not, the legacy db is left
    alone — it's shared across profiles and overwriting it would corrupt
    other profiles' state. The profile gets a fresh db on first use.
    """
    home = resolve_obscura_home()
    if profile_id:
        return home / "profiles" / profile_id
    return home


# ---------------------------------------------------------------------------
# Session orchestrator
# ---------------------------------------------------------------------------


class ObscuraSession:
    """High-level session wrapping ObscuraClient with tools, memory, and KAIROS.

    Use :meth:`create` to build a fully-initialised session, then call
    :meth:`send` to exchange messages.  Call :meth:`close` when done.
    """

    # Populated by create()
    _config: SessionConfig
    _profile_home: Path
    _store: SQLiteEventStore
    _sid: str
    _ctx: REPLContext
    _client: ObscuraClient
    _client_cm: Any  # the async-context-manager wrapper
    _vector_store: Any
    _context_router: Any
    _turn_classifier: Any
    _combined_system: str
    _loop_kwargs: dict[str, Any]
    _tool_count: int
    _kairos_engine: Any
    _kairos_hooks_registered: bool
    _tip_scheduler: Any
    _supervisor: Any
    _supervisor_task: asyncio.Task[None] | None
    _daemon_task: asyncio.Task[None] | None
    _uds_inbox: Any
    _background_tasks: set[asyncio.Task[str]]

    # ── Factory ────────────────────────────────────────────────────────────

    @classmethod
    async def create(cls, config: SessionConfig) -> ObscuraSession:
        """Build a fully-initialised session.

        Performs: env loading, tool/MCP discovery, vector memory init,
        system prompt composition, tool assembly, callback wiring, hooks,
        client creation, and session resume.
        """
        self = cls()
        self._config = config
        self._supervisor = None
        self._supervisor_task = None
        self._daemon_task = None
        self._uds_inbox = None
        self._kairos_engine = None
        self._kairos_hooks_registered = False
        self._tip_scheduler = None
        self._background_tasks = set()

        # Profile-scoped storage. ``profile_id`` is supplied by the
        # browser-extension native host (one host process per Chrome
        # profile); terminal REPL leaves it as ``None`` and keeps the
        # legacy shared paths.
        self._profile_home = _resolve_profile_home(config.profile_id)
        self._store = SQLiteEventStore(self._profile_home / "events.db")
        self._sid = config.session_id or uuid.uuid4().hex

        await self._load_env(config)
        mcp_configs, mcp_names = self._discover_tools(config)
        self._init_vector_memory()
        combined_system = self._compose_system_prompt(config)
        self._combined_system = combined_system
        system_tools, tool_count = self._assemble_tools(config)
        self._tool_count = tool_count
        project_hooks = self._wire_callbacks_and_hooks(config, system_tools)

        # Build client
        client = ObscuraClient(
            config.backend,
            model=config.model,
            system_prompt=combined_system,
            tools=system_tools or None,
            mcp_servers=mcp_configs or None,
            hooks=project_hooks,
        )
        self._client_cm = client
        await client.__aenter__()
        self._client = client

        # Tool router
        self._wire_tool_router(config)

        # Session resume
        resume_summary = await self._try_resume(config)

        # Build loop kwargs
        loop_kwargs: dict[str, Any] = {}
        if not config.tools_enabled:
            loop_kwargs["tool_choice"] = ToolChoice.none()
        self._loop_kwargs = loop_kwargs

        # Build REPL context
        self._ctx = REPLContext(
            client=client,
            store=self._store,
            session_id=self._sid,
            backend=config.backend,
            model=config.model,
            system_prompt=combined_system,
            max_turns=config.max_turns,
            tools_enabled=config.tools_enabled,
            mcp_configs=mcp_configs,
            confirm_enabled=config.confirm,
            vector_store=self._vector_store,
            _context_router=self._context_router,
            _turn_classifier=self._turn_classifier,
        )

        # Hydrate context from prior session when backend resume failed
        if resume_summary:
            self._ctx.message_history.append(
                (
                    "user",
                    f"[RESUMED SESSION CONTEXT]\n\n{resume_summary}",
                )
            )
            try:
                from obscura.cli.render import print_info

                print_info(
                    f"Loaded session summary ({len(resume_summary)} chars)",
                )
            except Exception:
                pass

        return self

    # ── Public API ─────────────────────────────────────────────────────────

    @property
    def ctx(self) -> REPLContext:
        """The mutable REPL context (tools, history, mode, etc.)."""
        return self._ctx

    @property
    def client(self) -> ObscuraClient:
        return self._client

    @property
    def session_id(self) -> str:
        return self._sid

    @property
    def store(self) -> SQLiteEventStore:
        return self._store

    @property
    def tool_count(self) -> int:
        return self._tool_count

    @property
    def loop_kwargs(self) -> dict[str, Any]:
        return self._loop_kwargs

    @property
    def kairos_engine(self) -> Any:
        return self._kairos_engine

    @kairos_engine.setter
    def kairos_engine(self, value: Any) -> None:
        self._kairos_engine = value

    @property
    def tip_scheduler(self) -> Any:
        return self._tip_scheduler

    @tip_scheduler.setter
    def tip_scheduler(self, value: Any) -> None:
        self._tip_scheduler = value

    async def send(
        self,
        text: str,
        *,
        streaming_status: Any | None = None,
        loop_kwargs: dict[str, Any] | None = None,
        images: list[Any] | None = None,
        attached_files: list[dict[str, Any]] | None = None,
    ) -> str:
        """Send a chat message and stream the response.

        Returns the accumulated assistant text.  All pre/post-processing
        (vector memory, auto-compact, auto-title, plan parsing) is included.

        ``images`` accepts a list of either data URLs (``data:image/png;
        base64,…``), raw base64 strings, or dicts with ``data``/``dataUrl``/
        ``base64`` and optional ``media_type``. They are forwarded to the
        backend as multimodal content blocks (Claude); other backends
        receive the plain prompt and a textual marker per image.

        ``attached_files`` accepts a list of ``{name, content}`` dicts and is
        prepended to the prompt as ``<file path="…">…</file>`` blocks.
        """
        from obscura.cli import trace as trace_mod

        ctx = self._ctx
        effective_kwargs = loop_kwargs if loop_kwargs is not None else self._loop_kwargs

        # ── Attached files: prepend as tagged blocks before the user prompt.
        if attached_files:
            file_blocks: list[str] = []
            for f in attached_files:
                try:
                    name = str(f.get("name") or "attachment")
                    content = str(f.get("content") or "")
                except Exception:
                    continue
                if not content:
                    continue
                # Escape the closing tag so file content can't break out.
                safe = content.replace("</file>", "<\u2009/file>")
                file_blocks.append(f'<file path="{name}">\n{safe}\n</file>')
            if file_blocks:
                text = "\n\n".join(file_blocks) + "\n\n" + text

        # ── Images: append a textual marker per image so backends without
        # vision plumbing still know images were referenced. The actual
        # image data is forwarded via ``run_loop(images=…)`` below.
        normalized_images: list[Any] = []
        if images:
            markers: list[str] = []
            for i, item in enumerate(images):
                name = ""
                if isinstance(item, dict):
                    from typing import cast as _cast

                    item_dict = _cast("dict[str, Any]", item)
                    raw_name = item_dict.get("name") or item_dict.get("filename")
                    if isinstance(raw_name, str):
                        name = raw_name
                if not name:
                    name = f"image-{i + 1}"
                markers.append(f"[image: {name}]")
                normalized_images.append(item)
            if markers:
                marker_block = " ".join(markers)
                text = f"{text}\n\n{marker_block}" if text else marker_block

        # Inline agent check
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
        from obscura.core.types import ToolCallInfo

        async def confirm_cb(tc: ToolCallInfo) -> bool:
            try:
                from obscura.core.permission_modes import (
                    PermissionMode,
                    PermissionModeEngine,
                )

                mode_str = getattr(ctx, "_permission_mode", "default")
                engine = PermissionModeEngine(PermissionMode(mode_str))
                decision = engine.evaluate(tc.name, tc.input)
                if not decision.allowed:
                    print_warning(f"Blocked by {mode_str} mode: {tc.name}")
                    return False
                if decision.auto_approved:
                    return True
            except Exception:
                pass
            if ctx.confirm_enabled:
                return await _cli_confirm(ctx, tc.name, tc.input)
            return True

        # ── Token-aware auto-compact ──────────────────────────────────────
        from obscura.cli.commands import cmd_compact, estimate_effective_context_tokens

        _context_window = ctx.client.context_window
        _compact_threshold = int(_context_window * 0.60)
        _warn_threshold = ctx.client.context_warn_threshold

        from obscura.tools.system import update_token_usage

        # ── Vector memory pre-search ──────────────────────────────────────
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

        _tip_scheduler = self._tip_scheduler

        # ── Streaming with graceful retry ─────────────────────────────────
        async def _stream_with_retry(
            context_retry_used: bool = False,
            dead_session_retry_used: bool = False,
        ) -> list[str]:
            nonlocal _stream_output_chars
            _buf: list[str] = []
            _effective_kwargs = dict(effective_kwargs)
            if hasattr(ctx, "_effort_level") and ctx._effort_level:
                try:
                    from obscura.core.types import EFFORT_THINKING_BUDGETS, EffortLevel

                    _lvl = EffortLevel(ctx._effort_level)
                    _effective_kwargs["max_thinking_tokens"] = EFFORT_THINKING_BUDGETS[
                        _lvl
                    ]
                except (ValueError, KeyError):
                    pass
            # Forward multimodal images so the Claude backend can attach them
            # as content blocks. Other backends receive the kwarg through
            # ``**kwargs`` and silently ignore it (a textual ``[image: name]``
            # marker is already in the prompt as a fallback for them).
            if normalized_images:
                _effective_kwargs["images"] = normalized_images
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
                        _stream_output_chars += len(event.text)
                        _push_stream_token_usage()
                    _track_file_event(event.kind, ctx, event)
                    # Deep logging
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
                                getattr(event, "tool_input", {}),
                                ok=not getattr(event, "is_error", False),
                                result_preview=str(getattr(event, "tool_result", ""))[
                                    :200
                                ],
                            )
                    except Exception:
                        pass
                    # Tool output collapsing
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
                        # Tips
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
                        collapser = getattr(ctx, "_collapser", None)
                        if collapser is not None and collapser.pending:
                            try:
                                summary = collapser.flush_summary()
                                if summary:
                                    console.print(f"[dim]  {summary}[/]")
                            except Exception:
                                pass
                    # Cost tracking
                    if event.kind in (
                        AgentEventKind.TURN_COMPLETE,
                        AgentEventKind.AGENT_DONE,
                    ):
                        meta = getattr(event, "metadata", None)
                        if meta is not None:
                            _usage = getattr(meta, "usage", None) or {}
                            if isinstance(_usage, dict):
                                inp = _usage.get("input_tokens", 0) or 0
                                out = _usage.get("output_tokens", 0) or 0
                            else:
                                inp = getattr(_usage, "input_tokens", 0) or 0
                                out = getattr(_usage, "output_tokens", 0) or 0
                            if inp > 0 or out > 0:
                                try:
                                    from obscura.core.cost_tracker import (
                                        get_cost_tracker,
                                    )

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

        # Vector memory auto-save
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

        # Post-send: update token tracker
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

        # Auto-compact
        if _post_tokens > _compact_threshold:
            try:
                from obscura.core.compaction import should_auto_compact

                if should_auto_compact(
                    [{"role": r, "content": t} for r, t in ctx.message_history],
                    ctx.model or "default",
                    system_prompt=ctx.system_prompt,
                ):
                    console.print("[dim cyan]  Auto-compacting context...[/]")
                    await cmd_compact("4", ctx)
            except Exception:
                pass

        # Auto-title
        global _session_state
        if not _session_state["titled"] and len(ctx.message_history) >= 2:
            _session_state["titled"] = True
            try:
                from obscura.core.session_utils import generate_session_title

                title = await generate_session_title(text, ctx.client._backend)
                if title:
                    await ctx.store.update_session(ctx.session_id, summary=title)
                    if (
                        hasattr(ctx, "_prompt_status")
                        and ctx._prompt_status is not None
                    ):
                        ctx._prompt_status.session_title = title
                    console.print(
                        f"  [dim]session titled:[/] [bold bright_cyan]{title}[/]",
                        highlight=False,
                    )
            except Exception:
                pass

        # Parse plan if in PLAN mode
        _maybe_parse_plan(response_text, ctx)

        # Auto-detect question choices
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
                        return await self.send(
                            selection,
                            streaming_status=streaming_status,
                            loop_kwargs=effective_kwargs,
                        )
        except Exception:
            pass

        return response_text

    async def handle_slash_command(self, raw: str) -> str | None:
        """Dispatch a slash command.  Returns ``'quit'`` if session should end."""
        return await handle_command(raw, self._ctx)

    async def close(self) -> None:
        """Teardown: stop supervisor, KAIROS, flush logs, close stores."""
        ctx = self._ctx

        # Stop supervisor fleet
        if self._supervisor_task is not None:
            if self._supervisor is not None:
                with contextlib.suppress(Exception):
                    await self._supervisor.stop()
            if not self._supervisor_task.done():
                self._supervisor_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await self._supervisor_task
        if self._daemon_task is not None:
            if not self._daemon_task.done():
                self._daemon_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await self._daemon_task
            dc = getattr(self._daemon_task, "_daemon_client", None)
            if dc is not None:
                with contextlib.suppress(Exception):
                    await dc.__aexit__(None, None, None)

        # Await background message tasks
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)

        # Stop runtime
        await ctx.stop_runtime()

        # Stop UDS inbox
        if self._uds_inbox is not None:
            with contextlib.suppress(Exception):
                await self._uds_inbox.stop()

        # Flush deep log
        try:
            from obscura.core.deep_log import dlog

            dlog.session_event("end", session_id=ctx.session_id)
            dlog.flush()
            dlog.close()
        except Exception:
            pass

        # KAIROS: stop engine if not already handled by supervisor hooks
        if self._kairos_engine is not None and not self._kairos_hooks_registered:
            with contextlib.suppress(Exception):
                await self._kairos_engine.stop()

        # Run cleanup tasks
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

        # Update session status
        try:
            sess = await self._store.get_session(self._sid)
            if sess is not None and sess.status == SessionStatus.RUNNING:
                await self._store.update_status(self._sid, SessionStatus.COMPLETED)
        except Exception:
            pass

        # Close client
        try:
            await self._client_cm.__aexit__(None, None, None)
        except Exception:
            pass

        self._store.close()

    # ── iMessage daemon ────────────────────────────────────────────────────

    async def start_imessage_daemon(self) -> asyncio.Task[None] | None:
        """Start iMessage daemon if configured in agents.yaml."""
        from obscura.agent.daemon_agent import DaemonAgent
        from obscura.agent.interaction import InteractionBus
        from obscura.agent.supervisor import SupervisorConfig
        from obscura.cli.render import console as _console

        config_path = Path.home() / ".obscura" / "agents.yaml"
        if not config_path.exists():
            return None

        cfg = SupervisorConfig.from_yaml(config_path)
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

            import logging as _logging

            _logging.getLogger("obscura.agent.daemon_agent").setLevel(_logging.WARNING)

            daemon_client = ObscuraClient(
                agent_def.model,
                system_prompt=agent_def.system_prompt,
            )
            await daemon_client.__aenter__()

            # Load persisted schedules
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
                daemon.loop_forever(),
                name=f"daemon-{agent_def.name}",
            )

            def _on_task_done(t: asyncio.Task[None]) -> None:
                exc = t.exception() if not t.cancelled() else None
                if exc:
                    _console.print(f"[red]Daemon task crashed: {exc}[/]")
                elif t.cancelled():
                    _console.print("[dim]Daemon task cancelled[/]")
                else:
                    _console.print("[dim]Daemon task completed[/]")

            task.add_done_callback(_on_task_done)
            task._daemon_client = daemon_client  # type: ignore[attr-defined]
            self._daemon_task = task
            return task

        return None

    # ── Private: initialization phases ─────────────────────────────────────

    async def _load_env(self, config: SessionConfig) -> None:
        """Load .env files (global → project-local → CWD)."""
        import os

        self._backend_name = config.backend or os.environ.get("OBSCURA_BACKEND", "")
        self._model_name = config.model or os.environ.get("OBSCURA_MODEL", "")

        try:
            from dotenv import load_dotenv

            from obscura.core.paths import resolve_obscura_global_home

            global_env = resolve_obscura_global_home() / ".env"
            if global_env.is_file():
                load_dotenv(global_env)

            local_env = resolve_obscura_home() / ".env"
            if local_env.is_file() and local_env.resolve() != global_env.resolve():
                load_dotenv(local_env)

            load_dotenv()
        except Exception:
            pass

    def _discover_tools(
        self,
        config: SessionConfig,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Discover MCP servers.  Returns (configs, names).

        Merges any ``config.extra_mcp_servers`` passed by the caller with
        the set auto-discovered from ``~/.obscura/mcp/``.  The injected
        entries are appended, so user-configured servers take precedence
        on name collisions via downstream deduplication if any.
        """
        if not config.tools_enabled:
            return [], []
        configs, names = _discover_mcp()
        for extra in config.extra_mcp_servers or []:
            configs.append(extra)
            name = str(extra.get("name") or "")
            if name:
                names.append(name)
        return configs, names

    def _init_vector_memory(self) -> None:
        """Initialize vector store and memory channels."""
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
        self._cli_user = cli_user

        # Scope the SQLite vector backend to the profile dir if a
        # profile_id is set and the env var hasn't been explicitly
        # overridden by the caller. The vector backend reads this env
        # var lazily on first instantiation, so set it before
        # ``init_vector_store`` runs. We don't migrate any existing
        # ``~/.obscura/vector_memory/`` data — the profile gets a fresh
        # store on first write, mirroring the events.db policy.
        if self._config.profile_id and "OBSCURA_VECTOR_MEMORY_DIR" not in os.environ:
            os.environ["OBSCURA_VECTOR_MEMORY_DIR"] = str(
                self._profile_home / "vector_memory",
            )

        self._vector_store = init_vector_store(cli_user)

        if self._vector_store is not None:
            run_startup_maintenance(self._vector_store)

        self._context_router = None
        self._turn_classifier = None
        if self._vector_store is not None:
            try:
                from obscura.memory_channels import (
                    ContextRouter,
                    TurnClassifier,
                    load_channels_from_config,
                )

                _channels = load_channels_from_config()
                if _channels:
                    self._context_router = ContextRouter(_channels, self._vector_store)
                    self._turn_classifier = TurnClassifier(_channels)
            except Exception:
                pass

    def _compose_system_prompt(self, config: SessionConfig) -> str:
        """Build the combined system prompt."""
        import os

        from obscura.core.context import load_obscura_memory
        from obscura.core.system_prompts import (
            compose_environment_context,
            compose_system_prompt,
        )

        include_default = not config.no_default_prompt
        if os.environ.get("OBSCURA_INCLUDE_DEFAULT_PROMPT", "true").lower() == "false":
            include_default = False

        # Reuse the profile-scoped events.db computed in ``create()``
        # so memory loading sees the same store the session writes to.
        db_path = self._profile_home / "events.db"
        memory_context = load_obscura_memory(self._sid, db_path)
        custom_sections: list[str] = [memory_context] if memory_context else []

        # User identity & preferences
        prefs_path = resolve_obscura_home() / "memory" / "preferences.md"
        if prefs_path.exists():
            prefs_text = prefs_path.read_text().strip()
            if prefs_text:
                custom_sections.append(
                    f"# User Identity & Preferences\n\n{prefs_text}",
                )

        # Vector memory context at session start
        if self._vector_store is not None:
            vm_startup = load_startup_memories(self._vector_store, self._sid, top_k=3)
            if vm_startup:
                custom_sections.append(vm_startup)

        # Memory channel documentation
        if self._context_router is not None:
            try:
                from obscura.tools.memory_tools import build_channels_prompt_section

                channels_doc = build_channels_prompt_section(
                    self._context_router.channels,
                )
                if channels_doc:
                    custom_sections.append(channels_doc)

                sys_channel_ctx = self._context_router.get_system_channels()
                if sys_channel_ctx:
                    custom_sections.append(sys_channel_ctx)
            except Exception:
                pass

        # Environment context
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
            pass

        # KAIROS context
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

        # Coordinator context
        try:
            from obscura.agent.coordinator import (
                get_coordinator_system_prompt,
                is_coordinator_mode,
            )

            if is_coordinator_mode():
                custom_sections.append(get_coordinator_system_prompt())

                try:
                    from obscura.tools.swarm import (
                        build_agent_catalog,
                        load_agent_configs,
                    )

                    catalog = build_agent_catalog(load_agent_configs())
                    if catalog:
                        custom_sections.append(
                            f"## Available Specialist Agents\n\n{catalog}",
                        )
                except Exception:
                    pass
        except Exception:
            pass

        return compose_system_prompt(
            base=config.system_prompt,
            include_default=include_default,
            custom_sections=custom_sections or None,
        )

    def _assemble_tools(
        self,
        config: SessionConfig,
    ) -> tuple[list[Any], int]:
        """Gather and filter system tools.  Returns (tools, count)."""
        system_tools: list[Any] = []
        if not config.tools_enabled:
            return system_tools, 0

        try:
            from obscura.tools.system import get_system_tool_specs

            system_tools = get_system_tool_specs()
        except Exception:
            pass

        # Memory tools
        if self._vector_store is not None:
            try:
                from obscura.tools.memory_tools import make_memory_tool_specs

                system_tools.extend(make_memory_tool_specs(self._cli_user))
            except Exception:
                pass

        # Worktree tools
        try:
            from obscura.tools.worktree import get_worktree_tool_specs

            system_tools.extend(get_worktree_tool_specs())
        except Exception:
            pass

        # Task tools
        try:
            from obscura.tools.task_tools import get_task_tool_specs

            system_tools.extend(get_task_tool_specs())
        except Exception:
            pass

        # Goal tools
        try:
            from obscura.tools.goal_tools import get_goal_tool_specs

            system_tools.extend(get_goal_tool_specs())
        except Exception:
            pass

        # Arbiter tools
        try:
            from obscura.tools.arbiter_tools import get_arbiter_tool_specs

            system_tools.extend(get_arbiter_tool_specs())
        except Exception:
            pass

        # Profile tools
        try:
            from obscura.tools.profile_tools import get_profile_tool_specs

            system_tools.extend(get_profile_tool_specs())
        except Exception:
            pass

        # LSP tool
        try:
            from obscura.tools.lsp import get_lsp_tool_specs

            system_tools.extend(get_lsp_tool_specs())
        except Exception:
            pass

        # Browser tool
        try:
            from obscura.tools.browser import get_browser_tool_specs

            system_tools.extend(get_browser_tool_specs())
        except Exception:
            pass

        # Builtin plugin tools
        try:
            existing_names = {t.name for t in system_tools}
            _ws_include = (
                getattr(config.compiled_ws, "plugin_include", None)
                if config.compiled_ws
                else None
            )
            _ws_exclude = (
                getattr(config.compiled_ws, "plugin_exclude", None)
                if config.compiled_ws
                else None
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

        # Backfill capability metadata
        if system_tools:
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

        # Filter by capability grants
        if system_tools:
            try:
                from obscura.plugins.capabilities import (
                    resolve_allowed_tools_from_config,
                )

                _allowed = resolve_allowed_tools_from_config()
                if _allowed is not None:
                    system_tools = [
                        t
                        for t in system_tools
                        if not getattr(t, "capability", "") or t.name in _allowed
                    ]
            except Exception:
                pass

        return system_tools, len(system_tools)

    def _wire_callbacks_and_hooks(
        self,
        config: SessionConfig,
        system_tools: list[Any],
    ) -> Any:
        """Wire ask_user, permission, plan callbacks and project hooks."""
        # We need a forward reference to ctx for callbacks.
        # The callbacks capture `self` and access self._ctx at call time.

        if config.tools_enabled:
            # ask_user callback
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

            # Permission mode + plan approval callbacks
            try:
                from obscura.tools.system import (
                    set_permission_mode_callback,
                    set_plan_approval_callback,
                )

                def _set_permission_mode(mode: str) -> None:
                    self._ctx._permission_mode = mode

                set_permission_mode_callback(_set_permission_mode)

                async def _plan_approval_handler(plan_summary: str) -> bool:
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

            # user_interact callback
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
                        selected = [
                            s.strip() for s in result.text.split(",") if s.strip()
                        ]
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

        # Project hooks
        project_hooks = None
        try:
            from obscura.core.settings import load_all_hooks

            _hook_registry = load_all_hooks()
            if _hook_registry.count > 0:
                project_hooks = _hook_registry
        except Exception:
            pass

        # Memory channel TOOL_CALL hook
        self._tool_router_ref = None
        if self._context_router is not None:
            from obscura.core.hooks import HookRegistry
            from obscura.core.types import AgentEventKind as _AEK

            if project_hooks is None:
                project_hooks = HookRegistry()

            _context_router = self._context_router

            def _channel_tool_signal(event: Any) -> None:
                _context_router.update_signals_from_event(event)
                if (
                    self._tool_router_ref is not None
                    and _context_router.signals.file_paths
                ):
                    self._tool_router_ref.set_file_context(
                        list(_context_router.signals.file_paths),
                    )

            project_hooks.add_after(_channel_tool_signal, _AEK.TOOL_CALL)

        # Kairos hooks
        try:
            from obscura.kairos.engine import is_kairos_enabled as _kie2

            if _kie2():
                from obscura.core.hooks import HookRegistry
                from obscura.core.types import AgentEventKind as _AEK2

                if project_hooks is None:
                    project_hooks = HookRegistry()

                def _kairos_tool_hook(event: Any) -> None:
                    if (
                        self._kairos_engine is not None
                        and self._kairos_engine.is_running
                    ):
                        tool = getattr(event, "tool_name", "") or ""
                        args = str(getattr(event, "tool_input", "") or "")[:80]
                        self._kairos_engine.log_tool_use(tool, args)

                def _kairos_turn_hook(event: Any) -> None:
                    if (
                        self._kairos_engine is not None
                        and self._kairos_engine.is_running
                    ):
                        self._kairos_engine.log_agent_event("turn_complete")

                project_hooks.add_after(_kairos_tool_hook, _AEK2.TOOL_CALL)
                project_hooks.add_after(_kairos_turn_hook, _AEK2.TURN_COMPLETE)
        except Exception:
            pass

        return project_hooks

    def _wire_tool_router(self, config: SessionConfig) -> None:
        """Wire eval-driven tool router after client creation."""
        if not config.tools_enabled:
            return
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
                backend=config.backend,
            )
            self._client._backend.set_tool_router(_router)
            self._tool_router_ref = _router
        except Exception:
            pass

    async def _try_resume(self, config: SessionConfig) -> str:
        """Attempt session resume; returns a summary string on failure."""
        if not config.session_id:
            return ""
        try:
            await self._client.resume_session(
                SessionRef(
                    session_id=config.session_id,
                    backend=Backend(config.backend),
                ),
            )
        except Exception as exc:
            print_warning(
                f"Resume failed for session {config.session_id[:12]}: {exc}. "
                "Starting a fresh backend session.",
            )
            with contextlib.suppress(Exception):
                await self._client.reset_session()
            try:
                from obscura.core.context import summarize_session_for_resume

                return summarize_session_for_resume(
                    config.session_id,
                    self._profile_home / "events.db",
                )
            except Exception:
                pass
        return ""


# ---------------------------------------------------------------------------
# CLI-specific: tool confirmation via TUI widget
# ---------------------------------------------------------------------------


async def _cli_confirm(
    ctx: REPLContext,
    tool_name: str,
    tool_input: dict[str, Any],
) -> bool:
    """Prompt user to approve a tool call via TUI widget.  Returns True to allow."""
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
