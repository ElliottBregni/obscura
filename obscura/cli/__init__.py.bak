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
import time
import uuid
import re
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
    PromptStatus,
    StreamingStatus,
    _get_git_branch,
    animate_spinner,
    bordered_prompt,
    confirm_prompt_async,
    create_prompt_session,
)
from obscura.cli.render import (
    StreamRenderer,
    console,
    print_error,
    print_banner,
    print_warning,
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


def _discover_agents() -> list[str]:
    """Read agent names from ~/.obscura/agents.yaml without instantiating AgentRuntime.

    Fully lazy — just parses YAML names, deduplicates, returns list.
    Returns empty list on any error.
    """
    try:
        import yaml  # type: ignore[import-untyped]

        agents_yaml = resolve_obscura_home() / "agents.yaml"
        if not agents_yaml.exists():
            return []
        with open(agents_yaml) as f:
            config = yaml.safe_load(f) or {}
        seen: set[str] = set()
        names: list[str] = []
        for agent in config.get("agents", []):
            name = agent.get("name", "")
            if name and name not in seen:
                seen.add(name)
                names.append(name)
        return names
    except Exception:
        return []


_INLINE_AGENT_MENTION_RE = re.compile(
    r"^\s*@(?P<name>[A-Za-z0-9][A-Za-z0-9_-]*)\s+(?P<prompt>.+?)\s*$",
    re.DOTALL,
)


def _parse_inline_agent_mention(text: str) -> tuple[str, str] | None:
    """Parse ``@agent <prompt>`` syntax from a user message."""
    match = _INLINE_AGENT_MENTION_RE.match(text)
    if not match:
        return None
    agent_name = match.group("name").strip()
    prompt = match.group("prompt").strip()
    if not agent_name or not prompt:
        return None
    return agent_name, prompt


async def _run_inline_agent_from_mention(ctx: REPLContext, text: str) -> str | None:
    """Execute ``@agent <prompt>`` inline using manifest config when available."""
    parsed = _parse_inline_agent_mention(text)
    if parsed is None:
        return None

    agent_name, prompt = parsed
    runtime = await ctx.get_runtime()

    from obscura.manifest.models import AgentManifest
    from obscura.core.paths import resolve_obscura_home
    from obscura.cli.render import LabeledStreamRenderer

    manifest: AgentManifest | None = None
    agents_yaml = resolve_obscura_home() / "agents.yaml"
    if agents_yaml.exists():
        try:
            import yaml

            with open(agents_yaml) as f:
                config = yaml.safe_load(f) or {}
            agent_configs = {
                a.get("name", ""): a
                for a in config.get("agents", [])
                if isinstance(a, dict)
            }
            cfg = agent_configs.get(agent_name)
            if cfg is not None:
                if cfg.get("type") == "daemon":
                    print_warning(
                        f"@{agent_name} is a daemon agent and cannot be invoked inline."
                    )
                    return ""
                skills_cfg = cfg.get("skills", {})
                if not isinstance(skills_cfg, dict):
                    skills_cfg = {}
                raw_mcp_servers = cfg.get("mcp_servers", [])
                parsed_mcp_servers: list[dict[str, Any]] = []
                if isinstance(raw_mcp_servers, list):
                    for server in raw_mcp_servers:
                        if isinstance(server, dict):
                            parsed_mcp_servers.append(server)
                        elif isinstance(server, str) and server.strip():
                            parsed_mcp_servers.append({"name": server.strip()})

                manifest = AgentManifest(
                    name=str(cfg.get("name", agent_name)),
                    provider=str(
                        cfg.get("provider") or cfg.get("model", ctx.backend)
                    ),
                    system_prompt=str(cfg.get("system_prompt", "")),
                    max_turns=int(cfg.get("max_turns", ctx.max_turns)),
                    tools=list(cfg.get("tools", []))
                    if isinstance(cfg.get("tools"), list)
                    else [],
                    tags=list(cfg.get("tags", []))
                    if isinstance(cfg.get("tags"), list)
                    else [],
                    mcp_servers=parsed_mcp_servers,
                    skills_config=skills_cfg,
                    can_delegate=bool(cfg.get("can_delegate", False)),
                    delegate_allowlist=list(cfg.get("delegate_allowlist", []))
                    if isinstance(cfg.get("delegate_allowlist"), list)
                    else [],
                    max_delegation_depth=int(cfg.get("max_delegation_depth", 3)),
                    tool_allowlist=list(cfg.get("tool_allowlist", []))
                    if isinstance(cfg.get("tool_allowlist"), list)
                    else None,
                )
        except Exception as exc:
            print_warning(f"Failed loading @{agent_name} manifest: {exc}")

    if manifest is None:
        print_warning(
            f"No manifest found for @{agent_name}; running with SDK defaults."
        )
        agent = runtime.spawn(
            agent_name,
            model=ctx.backend,
            system_prompt="",
        )
    else:
        agent = runtime.spawn_from_manifest(manifest, provider_override=ctx.backend)
    await agent.start()

    renderer = LabeledStreamRenderer(agent_name, "cyan")
    output_chunks: list[str] = []
    try:
        async for event in agent.stream_loop(prompt):
            renderer.handle(event)
            if getattr(event, "text", None):
                output_chunks.append(event.text)
    except KeyboardInterrupt:
        renderer.finish()
        console.print("[dim][interrupted][/]")
    except Exception as exc:
        renderer.finish()
        print_error(f"Inline @{agent_name} failed: {exc}")
    else:
        renderer.finish()
    finally:
        try:
            await agent.stop()
        except Exception:
            pass
    console.print()
    return "".join(output_chunks).strip()


# ---------------------------------------------------------------------------
# Tool confirmation callback
# ---------------------------------------------------------------------------


async def _cli_confirm(ctx: REPLContext, tool_name: str, tool_input: dict[str, Any]) -> bool:
    """Prompt user to approve a tool call via TUI widget. Returns True to allow."""
    if tool_name in ctx.confirm_always:
        return True

    from obscura.cli.widgets import ToolConfirmRequest, confirm_tool

    result = await confirm_tool(
        ToolConfirmRequest(tool_name=tool_name, tool_input=tool_input)
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
            )
        return inline_agent_response

    renderer = StreamRenderer(streaming_status=streaming_status)
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
            f"60% of {_context_window:,}) …[/]"
        )
        await cmd_compact("6", ctx)

    # ── Streaming with graceful retry on context-limit errors ────────────────
    async def _stream_with_retry(
        context_retry_used: bool = False, dead_session_retry_used: bool = False
    ) -> list[str]:
        nonlocal _stream_output_chars
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
                    _stream_output_chars += len(event.text)
                    _push_stream_token_usage()
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
            _is_dead_session_err = any(
                kw in _err
                for kw in (
                    "dead process",
                    "cannot send message",
                    "can't send message",
                    "session is closed",
                    "session closed",
                )
            )
            if _is_ctx_err and not context_retry_used:
                console.print(
                    "[red]⚠ Context limit reached — aggressive compact and retry…[/]"
                )
                await cmd_compact("2", ctx)
                return await _stream_with_retry(
                    context_retry_used=True,
                    dead_session_retry_used=dead_session_retry_used,
                )
            if _is_dead_session_err and not dead_session_retry_used:
                console.print(
                    "[yellow]⚠ Backend session became stale — resetting and retrying once…[/]"
                )
                try:
                    await ctx.client.reset_session()
                except Exception:
                    pass
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
            f"Auto-compact at {_compact_threshold:,} (60%).[/]"
        )

    # Parse plan if in PLAN mode
    _maybe_parse_plan(response_text, ctx)

    # Auto-detect question choices and present interactive widget
    try:
        from obscura.cli.widgets import detect_question_choices, present_detected_choices

        detected = detect_question_choices(response_text)
        if detected is not None:
            selection = await present_detected_choices(detected)
            if selection is not None:
                # Feed the selection back as a user message
                return await send_message(ctx, selection, loop_kwargs, streaming_status)
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
    from obscura.agent.supervisor import SupervisorConfig
    from obscura.agent.daemon_agent import DaemonAgent
    from obscura.agent.interaction import InteractionBus
    from obscura.cli.render import console as _console
    from obscura.core.client import ObscuraClient

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
            triggers.append(_IMT(
                contacts=tuple(im_cfg.get("contacts", [])),
                poll_interval=im_cfg.get("poll_interval", 30),
                notify_user=tdef.notify_user,
                priority=tdef.priority,
                data=im_data,
            ))

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

        daemon = DaemonAgent(daemon_client, name=agent_def.name, triggers=triggers)
        daemon._bus = bus  # type: ignore[attr-defined]

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
        task._daemon_client = daemon_client  # type: ignore[attr-defined]
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
                    ModelQuestionRequest,
                    ask_model_question,
                    confirm_attention,
                    AttentionWidgetRequest,
                )

                if choices:
                    result = await confirm_attention(
                        AttentionWidgetRequest(
                            request_id="ask_user",
                            agent_name="assistant",
                            message=question,
                            priority="normal",
                            actions=tuple(choices),
                        )
                    )
                    return result.action
                else:
                    result = await ask_model_question(
                        ModelQuestionRequest(question=question)
                    )
                    return result.text

            set_ask_user_callback(_ask_user_handler)
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
            except Exception as exc:
                # Keep the local session timeline but recover backend state.
                print_warning(
                    f"Resume failed for session {session_id[:12]}: {exc}. "
                    "Starting a fresh backend session."
                )
                try:
                    await client.reset_session()
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
        ss = StreamingStatus()

        # Lazy agent discovery — reads agents.yaml names only, no runtime created
        available_agents = _discover_agents() or None

        print_banner(
            backend,
            model,
            sid,
            tool_count=tool_count,
            mcp_servers=mcp_names or None,
            mode=mm.current.value,
            available_agents=available_agents,
        )

        # Start iMessage daemon if configured
        daemon_task: asyncio.Task[None] | None = None
        _daemon_client: Any = None
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

        def _refresh_prompt_status() -> None:
            """Refresh mutable fields of prompt_status before each prompt."""
            prompt_status.mode = mm.current.value
            prompt_status.model = ctx.model or ""
            # Collect running agents from runtime (if active)
            running: list[str] = []
            if ctx._runtime is not None:
                try:
                    from obscura.agent.agents import AgentStatus as _AS
                    for agent in ctx._runtime.list_agents(status=_AS.RUNNING):
                        running.append(agent.config.name)
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
            prompt_status.running_agents = running
            # Count active tasks (non-terminal agents + daemon)
            task_count = 0
            if ctx._runtime is not None:
                try:
                    from obscura.agent.agents import AgentStatus as _AS
                    _active = {_AS.RUNNING, _AS.WAITING, _AS.PENDING}
                    task_count += sum(
                        1 for a in ctx._runtime.list_agents()
                        if a.status in _active
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
        )

        # Background spinner animation for the toolbar
        spinner_task = asyncio.create_task(animate_spinner(ss))

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
                                try:
                                    await dc.__aexit__(None, None, None)
                                except Exception:
                                    pass
                            print_warning(
                                "iMessage daemon stopped unexpectedly; restarting "
                                f"(attempt {daemon_restart_count})"
                                + (f": {exc}" if exc else "")
                            )
                            try:
                                daemon_task = await _start_imessage_daemon(ctx.client)
                            except Exception as restart_exc:
                                print_warning(f"iMessage daemon restart failed: {restart_exc}")
                                daemon_task = None
                    _refresh_prompt_status()
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

                # Chat message: run in background so prompt stays responsive.
                # The StreamingStatus drives the toolbar spinner instead of
                # console.status() which conflicts with patch_stdout.
                task = asyncio.create_task(
                    send_message(ctx, user_input, loop_kwargs, streaming_status=ss)
                )
                background_tasks.add(task)

                def _on_done(t: asyncio.Task[str]) -> None:
                    background_tasks.discard(t)
                    ss.reset()

                task.add_done_callback(_on_done)

        finally:
            spinner_task.cancel()
            if daemon_task is not None:
                if not daemon_task.done():
                    daemon_task.cancel()
                    try:
                        await daemon_task
                    except (asyncio.CancelledError, Exception):
                        pass
                # Close the daemon's dedicated client
                dc = getattr(daemon_task, "_daemon_client", None)
                if dc is not None:
                    try:
                        await dc.__aexit__(None, None, None)
                    except Exception:
                        pass
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


@click.group(invoke_without_command=True)
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
@click.option(
    "--continue",
    "resume_last",
    is_flag=True,
    default=False,
    help="Resume the most recent session.",
)
@click.option("--resume", default=None, help="Resume session by ID (alias for --session).")
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
) -> None:
    """Obscura — AI agent REPL."""
    # If a subcommand was invoked, let Click handle it
    if ctx.invoked_subcommand is not None:
        return

    # Resolve session ID: --resume > --session > --continue (last session)
    resolved_session = resume or session
    if not resolved_session and resume_last:
        try:
            import sqlite3
            db_path = resolve_obscura_home() / "events.db"
            con = sqlite3.connect(str(db_path))
            row = con.execute(
                "SELECT id FROM sessions ORDER BY updated_at DESC LIMIT 1"
            ).fetchone()
            con.close()
            if row:
                resolved_session = row[0]
        except Exception:
            pass
    try:
        asyncio.run(
            _repl(backend, model, system, resolved_session, max_turns, tools, prompt, confirm, no_default_prompt)
        )
    except KeyboardInterrupt:
        pass  # graceful exit on Ctrl-C


@main.command()
@click.option("--force", is_flag=True, default=False, help="Reinitialise even if .obscura/ exists.")
def init(force: bool) -> None:
    """Initialise a local .obscura/ workspace in the current directory."""
    from obscura.core.workspace import WorkspaceExistsError, init_workspace

    try:
        ws = init_workspace(force=force)
        click.echo(f"Workspace initialised at {ws}")
    except WorkspaceExistsError:
        click.echo(".obscura/ already exists. Use --force to reinitialise.")
    except Exception as exc:
        click.echo(f"Init failed: {exc}", err=True)
