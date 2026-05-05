"""obscura.cli._repl_loop — Async REPL and session bootstrap.

Extracted from ``obscura/cli/__init__.py``.  This module owns the full
interactive session lifecycle:

  1. Startup (.env, secrets, tool discovery, system-prompt assembly)
  2. Client construction and session resume
  3. The ``while True`` input loop with slash-command dispatch,
     skill/command chaining, eval runner, and voice input
  4. Graceful teardown (supervisor, daemon, UDS inbox, deep log, cleanup)

Public API
----------
repl(backend, model, system, session_id, max_turns, tools, prompt, confirm,
     no_default_prompt, *, supervise, compiled_ws) -> None
    Core async loop.  Run via ``asyncio.run(repl(...))``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import subprocess
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from obscura.agent.agents import AgentStatus
from obscura.arbiter.hooks import register_agent_loop as _reg_arbiter_loop
from obscura.auth.cli_user import current_cli_user
from obscura.cli._env_loader import bootstrap_env
from obscura.cli._send import (
    _session_state,  # pyright: ignore[reportPrivateUsage]
    send_message,
)
from obscura.cli.bootstrap import (
    _discover_agent_infos,  # pyright: ignore[reportPrivateUsage]
    _discover_mcp,  # pyright: ignore[reportPrivateUsage]
)
from obscura.cli.commands import (
    COMMANDS,
    COMPLETIONS,
    REPLContext,
    estimate_effective_context_tokens,
    handle_command,
)
from obscura.cli.prompt import (
    PromptStatus,
    RunningAgentInfo,
    StreamingStatus,
    _get_git_branch,  # pyright: ignore[reportPrivateUsage]
    animate_spinner,
    bordered_prompt,
    create_prompt_session,
)
from obscura.cli.render import (
    console,
    print_banner,
    print_error,
    print_info,
    print_ok,
    print_warning,
)
from obscura.cli.tips import TipScheduler
from obscura.cli.tui_effects import ultrathink_banner
from obscura.composition.repl import build_repl_session
from obscura.composition.session import SessionConfig
from obscura.core.cleanup import cleanup_stale_files, register_cleanup, run_cleanup
from obscura.core.commit_attribution import get_attribution_tracker
from obscura.core.deep_log import dlog
from obscura.core.enums.agent import Backend
from obscura.core.enums.lifecycle import SessionStatus
from obscura.core.event_store import SQLiteEventStore
from obscura.core.paths import resolve_obscura_home
from obscura.core.prompt_cache import PromptCacheManager
from obscura.core.types import (
    SessionRef,
    ToolChoice,
)
from obscura.eval.models import EvalRunSummary
from obscura.eval.store import EvalResultStore
from obscura.kairos.away_summary import AwaySummaryTracker, generate_away_summary
from obscura.kairos.engine import is_kairos_enabled
from obscura.kairos.frustration import FrustrationDetector
from obscura.tools.system import Session
from obscura.tools.system._repl_commands import SlashBridge
from obscura.voice.session import VoiceSession

_log = logging.getLogger("obscura.cli")


def _swallow(label: str, exc: Exception) -> None:
    """Log a swallowed exception at DEBUG level."""
    _log.debug("%s: %s: %s", label, type(exc).__name__, exc)


async def repl(
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
    """Core async loop -- runs the interactive REPL or single-shot."""
    # Event store
    db_path = resolve_obscura_home() / "events.db"
    store = SQLiteEventStore(db_path)
    sid = session_id or uuid.uuid4().hex

    # Resolve backend/model names from arguments or environment defaults
    backend_name = backend or os.environ.get("OBSCURA_BACKEND", "")
    model_name = model or os.environ.get("OBSCURA_MODEL", "")

    # Load .env files and materialise secrets into os.environ.
    bootstrap_env()

    # Tool / MCP setup
    tools_enabled = tools == "on"
    mcp_configs: list[dict[str, Any]] = []
    mcp_names: list[str] = []
    if tools_enabled:
        mcp_configs, mcp_names = _discover_mcp()

    # Create authenticated user for vector memory + memory tools
    cli_user = current_cli_user()

    # Vector memory init + memory channel router moved to
    # obscura.composition.blocks.vector_memory.install_vector_memory
    # (runs inside build_repl_session below).

    # System-prompt composition moved to
    # obscura.composition.blocks.repl_prompt.install_repl_prompt_sections.
    # The base prompt is what the caller provided; the block reads
    # vector_store/context_router/etc. from the session and re-primes
    # the backend's system_prompt post-build.
    combined_system = system

    # System + plugin tool registration was extracted to the composition
    # layer (install_system_tools + install_plugin_tools). Both run inside
    # build_repl_session() below; tool_count is computed from the session's
    # registry after composition.

    # ask_user / plan_approval / user_interact callback wiring moved to
    # obscura.composition.blocks.repl_callbacks.install_repl_callbacks,
    # which runs inside build_repl_session. permission_mode is wired
    # inline below because its handler must mutate REPLContext (built
    # only after the session is constructed).
    #
    # Prompt composition (memory + preferences + user_memory + active
    # goals + channels + KAIROS + coordinator + WIZARD profile) moved to
    # obscura.composition.blocks.repl_prompt.install_repl_prompt_sections.
    # Tool gathering (system_tools + memory_tools + worktree/task/goal/
    # profile/lsp/browser/plugins) moved to
    # obscura.composition.blocks.system_tools.install_system_tools and
    # obscura.composition.blocks.plugins.install_plugin_tools. All run
    # inside build_repl_session() below.
    if tools_enabled:
        try:

            def _set_permission_mode(mode: str) -> None:
                ctx.permission_mode = mode  # set later

            Session.set_permission_mode_callback(_set_permission_mode)
        except Exception:
            _log.debug("suppressed exception in repl", exc_info=True)

    # Project hooks loading + memory-channel hook + KAIROS hooks moved to
    # obscura.composition.blocks.project_hooks.install_project_hooks.
    # The block reads session.context_router after install_vector_memory
    # runs, so the channel hook closure binds correctly.
    _tool_router_ref = None

    # Build session via composition. install_plugin_tools runs inside,
    # registering all plugin tool specs onto the session's registry +
    # backend, building the capability resolver, and storing it on the
    # session for the tool router below.
    # Discover agents.yaml entries so install_supervisor can pick them up
    agent_infos = _discover_agent_infos()
    available_agents = [a.name for a in agent_infos] or None

    _session_extras: dict[str, Any] = {
        "supervise": supervise,
        "agent_infos": agent_infos,
    }
    if compiled_ws is not None:
        _session_extras["compiled_ws"] = compiled_ws

    # Resolve active wizard profile to thread skill_filter through
    # SessionConfig.extras → install_skill_context (composition path).
    # Empty / None list disables filtering — load every discovered skill.
    try:
        from obscura.wizard import WizardService as _Wiz

        _active_profile = _Wiz().resolve_active_profile()
        if _active_profile is not None and _active_profile.skills:
            _session_extras["skill_filter"] = list(_active_profile.skills)
    except Exception:
        _log.debug("skill_filter resolution failed", exc_info=True)

    _session_config = SessionConfig(
        backend=backend,
        model=model,
        system_prompt=combined_system,
        tools_enabled=tools_enabled,
        confirm_enabled=confirm,
        max_turns=max_turns,
        mcp_servers=mcp_configs,
        extras=_session_extras,
    )
    _repl_session = await build_repl_session(
        _session_config,
        user=cli_user,
    )

    async with _repl_session as _session:
        client = _session.client
        tool_count = len(_session.registry.all())
        # Tool router wiring moved to obscura.composition.blocks.tool_router.
        # _tool_router_ref is preserved as a back-compat name for the
        # channel hook closure built earlier (which captured `nonlocal`
        # _tool_router_ref).
        _tool_router_ref = _session.tool_router

        # Session resume
        if session_id:
            try:
                await client.resume_session(
                    SessionRef(session_id=session_id, backend=Backend(backend)),
                )
            except Exception as exc:
                _log.debug("suppressed exception in repl", exc_info=True)
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
            vector_store=_session.vector_store,
            context_router=_session.context_router,
            turn_classifier=_session.turn_classifier,
        )

        # Install slash-command bridge so agent loop tools can run /init etc.
        try:

            async def _run_slash(name: str, arguments: str) -> tuple[str, str | None]:
                handler = COMMANDS.get(name)
                if handler is None:
                    raise KeyError(name)
                with console.capture() as cap:
                    ret = await handler(arguments, ctx)
                return cap.get(), ret

            SlashBridge.set_callback(_run_slash)
        except Exception:
            _log.debug("suppressed exception in repl", exc_info=True)

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
                    _log.debug("suppressed exception in repl", exc_info=True)
                store.close()
            return

        # --- Interactive REPL ---
        mm = ctx.get_mode_manager()
        ss = StreamingStatus()

        # Best-effort browser bridge attach
        browser_bridge_client: Any = None
        browser_status: dict[str, Any] | None = None
        # Browser bridge attach moved to obscura.composition.blocks.browser_bridge.
        # Read the post-build state off the session for the banner below.
        browser_bridge_client = _session.browser_bridge
        browser_status = (
            getattr(browser_bridge_client, "status", None)
            if browser_bridge_client is not None
            else None
        )
        if browser_status is not None:
            tool_count += int(browser_status.get("tool_count") or 0)

        print_banner(
            backend,
            model,
            sid,
            tool_count=tool_count,
            mcp_servers=mcp_names or None,
            mode=mm.current.value,
            available_agents=available_agents,
            agent_infos=agent_infos or None,
            browser_status=browser_status,
        )

        # Supervisor spawning moved to obscura.composition.blocks.supervisor.
        # Read post-build state for downstream code (daemon, banner) that
        # branches on whether the supervisor came up.
        supervisor = _session.supervisor
        supervisor_task = _session.supervisor_task
        if supervisor is not None and agent_infos:
            print_ok(f"Supervisor started -- {len(agent_infos)} agent(s) launching")

        # iMessage daemon spawn moved to obscura.composition.blocks.imessage_daemon.
        # Read post-build state for downstream code that polls the task.
        daemon_task: asyncio.Task[None] | None = _session.imessage_daemon_task
        _daemon_client: Any = None
        daemon_restart_count = 0
        daemon_last_restart_at = 0.0

        # Live status shown in the bottom toolbar
        prompt_status = PromptStatus(
            model=model or "",
            branch=_get_git_branch(),
            session_id=sid,
            mode=mm.current.value,
        )
        ctx._prompt_status = prompt_status  # type: ignore[attr-defined]

        def _refresh_prompt_status() -> None:
            prompt_status.mode = mm.current.value
            prompt_status.model = ctx.model or ""
            running: list[str] = []
            details: list[RunningAgentInfo] = []
            if ctx.runtime is not None:
                try:
                    _active = {
                        AgentStatus.RUNNING,
                        AgentStatus.WAITING,
                        AgentStatus.PENDING,
                    }
                    for agent in ctx.runtime.list_agents():
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
                    _log.debug(
                        "suppressed exception in _refresh_prompt_status", exc_info=True
                    )
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
            task_count = 0
            if ctx.runtime is not None:
                try:
                    _active2 = {
                        AgentStatus.RUNNING,
                        AgentStatus.WAITING,
                        AgentStatus.PENDING,
                    }
                    task_count += sum(
                        1 for a in ctx.runtime.list_agents() if a.status in _active2
                    )
                except Exception:
                    _log.debug(
                        "suppressed exception in _refresh_prompt_status", exc_info=True
                    )
            if daemon_task is not None and not daemon_task.done():
                task_count += 1
            prompt_status.task_count = task_count
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

        spinner_task = asyncio.create_task(animate_spinner(ss))

        # --- KAIROS integration ---
        # KAIROS engine init moved to obscura.composition.blocks.kairos.
        # Read post-build state for downstream hooks/closures that
        # captured `_kairos_engine` at parse time.
        _kairos_engine = _session.kairos_engine
        _kairos_hooks_registered = (
            _kairos_engine is not None
            and supervisor is not None
            and hasattr(supervisor, "hooks")
        )

        # Loop registration is intentional post-build wiring (the loop
        # only exists after the first stream_loop call begins). Keep it
        # here until install_session_registration extracts the per-stream
        # wiring.
        if _kairos_engine is not None:
            try:
                _agent_loop = getattr(client, "_loop", None)
                if _agent_loop is not None:
                    _kairos_engine.register_agent_loop(_agent_loop)
            except Exception as _e:
                _log.debug("suppressed exception in repl", exc_info=True)
                _swallow("kairos_loop_wire", _e)

        # Wire AgentLoop into Arbiter
        try:
            _al = getattr(client, "_loop", None)
            if _al is not None:
                _reg_arbiter_loop(_al)
        except Exception as _e:
            _log.debug("suppressed exception in repl", exc_info=True)
            _swallow("arbiter_loop_wire", _e)

        # --- Tips scheduler ---
        _tip_scheduler = None
        try:
            _tip_scheduler = TipScheduler()
        except Exception as _e:
            _log.debug("suppressed exception in repl", exc_info=True)
            _swallow("tips_init", _e)

        # --- Frustration detector ---
        _frustration_detector = None
        try:
            if is_kairos_enabled():
                _frustration_detector = FrustrationDetector()
        except Exception as _e:
            _log.debug("suppressed exception in repl", exc_info=True)
            _swallow("frustration_init", _e)

        # --- Away summary tracker ---
        _away_tracker = None
        try:
            if is_kairos_enabled():
                _away_tracker = AwaySummaryTracker()
        except Exception as _e:
            _log.debug("suppressed exception in repl", exc_info=True)
            _swallow("away_init", _e)

        # --- Prompt cache ---
        _prompt_cache = None
        try:
            _prompt_cache = PromptCacheManager()
        except Exception:
            _log.debug("suppressed exception in repl", exc_info=True)

        # --- Register cleanup tasks ---
        try:
            register_cleanup(
                "stale_files",
                lambda: cleanup_stale_files(max_age_days=30),
            )
        except Exception as _e:
            _log.debug("suppressed exception in repl", exc_info=True)
            _swallow("cleanup_init", _e)

        # Session registration (PID lock + signal handlers) moved to
        # obscura.composition.blocks.session_registration. Concurrent-
        # session warning is logged at INFO by the block.

        # --- Deep log session start ---
        try:
            dlog.session_event(
                "start",
                session_id=sid,
                backend=backend_name,
                model=model_name or "",
            )
        except Exception:
            _log.debug("suppressed exception in repl", exc_info=True)

        # UDS inbox spawn moved to obscura.composition.blocks.uds_inbox.
        # Reference for downstream slash commands.
        _uds_inbox = _session.uds_inbox

        # Reset session title tracker
        _session_state["titled"] = False

        background_tasks: set[asyncio.Task[str]] = set()
        try:
            while True:
                try:
                    if daemon_task is not None and daemon_task.done():
                        now = time.monotonic()
                        if now - daemon_last_restart_at >= 5.0:
                            daemon_last_restart_at = now
                            daemon_restart_count += 1
                            exc: BaseException | None = None
                            if not daemon_task.cancelled():
                                try:
                                    exc = daemon_task.exception()
                                except Exception:
                                    _log.debug(
                                        "suppressed exception in repl", exc_info=True
                                    )
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
                                from obscura.cli._daemon import (
                                    start_imessage_daemon,
                                )

                                daemon_task = await start_imessage_daemon(ctx.client)
                            except Exception as restart_exc:
                                _log.debug(
                                    "suppressed exception in repl", exc_info=True
                                )
                                print_warning(
                                    f"iMessage daemon restart failed: {restart_exc}",
                                )
                                daemon_task = None
                    _refresh_prompt_status()
                    user_input = await bordered_prompt(session, status=prompt_status)
                except (EOFError, KeyboardInterrupt):
                    _log.debug("suppressed exception in repl", exc_info=True)
                    console.print()
                    break
                if not user_input:
                    continue

                # Voice input
                if user_input == "__VOICE_RECORD__":
                    voice_enabled = getattr(ctx, "voice_enabled", False)
                    if not voice_enabled:
                        console.print(
                            "[dim]Voice mode is off. Enable with /voice on[/]"
                        )
                        continue
                    try:
                        _vsession = VoiceSession()
                        if not _vsession.is_available:
                            console.print(
                                f"[red]Voice unavailable: {_vsession.install_hint}[/]"
                            )
                            continue
                        console.print(
                            "[yellow]Recording... (speak now, press Enter when done)[/]"
                        )
                        await _vsession.start_recording()
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
                        _log.debug("suppressed exception in repl", exc_info=True)
                        console.print(f"[red]Voice error: {voice_exc}[/]")
                        continue

                # KAIROS: log user message
                if _kairos_engine is not None and _kairos_engine.is_running:
                    with contextlib.suppress(Exception):
                        _kairos_engine.log_user_message(user_input)

                # Ultrathink keyword detection
                if "ultrathink" in user_input.lower() and not user_input.startswith(
                    "/"
                ):
                    if getattr(ctx, "effort_level", "medium") != "max":
                        ctx.effort_level = "max"
                        try:
                            ultrathink_banner()
                        except Exception:
                            _log.debug("suppressed exception in repl", exc_info=True)
                            console.print(
                                "[bold bright_magenta]⚡ ULTRATHINK activated[/]"
                            )

                # Frustration detection
                if _frustration_detector is not None and not user_input.startswith("/"):
                    try:
                        _sentiment = _frustration_detector.analyze(user_input)
                        if (
                            _sentiment.is_frustrated
                            and _sentiment.consecutive_frustrations >= 2
                        ):
                            console.print(
                                "[dim italic]I notice some frustration -- "
                                "let me be more careful with my approach.[/]",
                            )
                    except Exception:
                        _log.debug("suppressed exception in repl", exc_info=True)

                # Away summary
                if _away_tracker is not None:
                    try:
                        if _away_tracker.should_generate():
                            _summary = await generate_away_summary(ctx.message_history)
                            if _summary:
                                console.print(f"[dim]{_summary}[/]")
                        _away_tracker.mark_active()
                    except Exception:
                        _log.debug("suppressed exception in repl", exc_info=True)

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

                # *eval -- benchmark a command/skill chain
                if user_input.startswith("*"):
                    _pe = print_error
                    _pi = print_info

                    def _snapshot_git() -> str | None:
                        try:
                            r = subprocess.run(
                                ["git", "diff", "--name-only", "HEAD"],
                                capture_output=True,
                                text=True,
                                timeout=5,
                            )
                            u = subprocess.run(
                                ["git", "ls-files", "--others", "--exclude-standard"],
                                capture_output=True,
                                text=True,
                                timeout=5,
                            )
                            files = (r.stdout.strip() + "\n" + u.stdout.strip()).strip()
                            return files or None
                        except Exception:
                            _log.debug(
                                "suppressed exception in _snapshot_git", exc_info=True
                            )
                            return None

                    def _revert_changes(before_files: str | None) -> list[str]:
                        try:
                            r = subprocess.run(
                                ["git", "diff", "--name-only", "HEAD"],
                                capture_output=True,
                                text=True,
                                timeout=5,
                            )
                            u = subprocess.run(
                                ["git", "ls-files", "--others", "--exclude-standard"],
                                capture_output=True,
                                text=True,
                                timeout=5,
                            )
                            after = set(
                                (r.stdout.strip() + "\n" + u.stdout.strip())
                                .strip()
                                .splitlines()
                            )
                            before = set(
                                before_files.splitlines() if before_files else []
                            )
                            new_files = after - before
                            reverted: list[str] = []
                            for f in sorted(new_files):
                                if not f:
                                    continue
                                cr = subprocess.run(
                                    ["git", "checkout", "HEAD", "--", f],
                                    capture_output=True,
                                    timeout=5,
                                )
                                if cr.returncode != 0:
                                    try:
                                        os.remove(f)
                                    except OSError:
                                        _log.debug(
                                            "suppressed exception in _revert_changes",
                                            exc_info=True,
                                        )
                                        continue
                                reverted.append(f)
                            return reverted
                        except Exception:
                            _log.debug(
                                "suppressed exception in _revert_changes", exc_info=True
                            )
                            return []

                    inner = user_input[1:].strip()
                    if not inner:
                        _pe("Usage: *@command [args] or *$skill @command [args]")
                        continue

                    skill_names, cmd_name, remaining = ctx.parse_chained_input(inner)

                    if cmd_name is None:
                        _pe("Eval requires an @command (e.g., *@review file.py)")
                        continue

                    if not remaining and not skill_names:
                        suite = ctx.get_eval_suite(cmd_name)
                        if suite is None:
                            _pe(
                                f"No eval suite found for @{cmd_name}. Create {cmd_name}.eval.md next to the command."
                            )
                            continue

                        _pi(
                            f"Running eval suite for @{cmd_name}: {len(suite.cases)} test case(s)"
                        )
                        total_pass = 0
                        total_criteria = 0

                        for case_idx, case in enumerate(suite.cases, 1):
                            _pi(f"\n-- Case {case_idx}/{len(suite.cases)}: {case.name}")
                            _pre_files = _snapshot_git()
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

                            if case.preferred_tools:
                                chain_blocks.append(
                                    "Preferred tools for this task: "
                                    + ", ".join(case.preferred_tools)
                                )

                            chain_input = "\n\n---\n\n".join(chain_blocks)
                            eval_kwargs = dict(loop_kwargs)
                            if resolved.meta.tools_enabled:
                                eval_kwargs.pop("tool_choice", None)

                            for run in range(suite.runs_per_case):
                                if suite.runs_per_case > 1:
                                    _pi(f"  Run {run + 1}/{suite.runs_per_case}")
                                response = await send_message(
                                    ctx, chain_input, eval_kwargs, streaming_status=ss
                                )
                                ss.reset()
                                grading = ctx.build_grading_prompt(
                                    cmd_name, case.input_args, response, case.criteria
                                )
                                _pi("  Grading...")
                                grade_response = await send_message(
                                    ctx, grading, loop_kwargs, streaming_status=ss
                                )
                                ss.reset()
                                total_criteria += len(case.criteria)
                                pass_count = grade_response.upper().count("| PASS")
                                total_pass += pass_count
                                if pass_count < len(case.criteria):
                                    reverted = _revert_changes(_pre_files)
                                    if reverted:
                                        _pe(
                                            f"  Eval failed ({pass_count}/{len(case.criteria)}) "
                                            f"-- reverted {len(reverted)} file(s): "
                                            + ", ".join(reverted)
                                        )
                                    else:
                                        _pe(
                                            f"  Eval failed ({pass_count}/{len(case.criteria)}) -- no file changes to revert"
                                        )

                        _pi(
                            f"\n-- Eval complete: {total_pass}/{total_criteria} criteria passed"
                        )
                        continue

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
                    eval_kwargs = dict(loop_kwargs)
                    if resolved.meta.tools_enabled:
                        eval_kwargs.pop("tool_choice", None)

                    _pi(f"*@{cmd_name}: running + grading")
                    _pre_files = _snapshot_git()
                    response = await send_message(
                        ctx, chain_input, eval_kwargs, streaming_status=ss
                    )
                    ss.reset()

                    cmd_criteria = getattr(resolved.meta, "eval_criteria", None)
                    criteria = cmd_criteria or [
                        "Response is relevant to the command's purpose",
                        "Response follows the command's output format",
                        "Response is complete (not truncated or missing sections)",
                        "Response is accurate (no hallucinated information)",
                        "Response is actionable (provides specific, useful details)",
                    ]
                    pass_threshold = getattr(
                        resolved.meta, "eval_pass_threshold", None
                    ) or len(criteria)
                    grading = ctx.build_grading_prompt(
                        cmd_name, remaining, response, criteria
                    )
                    _pi("Grading...")
                    grade_response = await send_message(
                        ctx, grading, loop_kwargs, streaming_status=ss
                    )
                    ss.reset()

                    try:
                        _pass_ct = grade_response.upper().count("| PASS")
                        _fail_ct = len(criteria) - _pass_ct
                        summary = EvalRunSummary(
                            run_id=f"cmd-{cmd_name}-{int(time.time())}",
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
                        eval_store = EvalResultStore()
                        asyncio.create_task(eval_store.save_run(summary))
                    except Exception:
                        _log.debug("suppressed exception in repl", exc_info=True)

                    pass_count = grade_response.upper().count("| PASS")
                    total = len(criteria)
                    _pi(
                        f"Score: {pass_count}/{total} (threshold: {pass_threshold}/{total})"
                    )
                    if pass_count < pass_threshold:
                        reverted = _revert_changes(_pre_files)
                        if reverted:
                            _pe(
                                f"Eval failed ({pass_count}/{total}) -- reverted {len(reverted)} file(s): "
                                + ", ".join(reverted)
                            )
                        else:
                            _pe(
                                f"Eval failed ({pass_count}/{total}) -- no file changes to revert"
                            )
                    else:
                        _pi(f"Eval passed ({pass_count}/{total}) -- changes kept")
                    continue

                # $skill / @command / chained input
                if user_input.startswith(("$", "@")):
                    _pe = print_error
                    _pi = print_info

                    skill_names, cmd_name, remaining = ctx.parse_chained_input(
                        user_input
                    )
                    blocks = []
                    _abort = False

                    for sname in skill_names:
                        body = ctx.resolve_dollar_skill(sname)
                        if body is None:
                            _pe(
                                f"Unknown skill: ${sname}. Available: {', '.join(ctx.discover_dollar_skills())}"
                            )
                            _abort = True
                            break
                        _pi(f"${sname}")
                        blocks.append(body)

                    if _abort:
                        continue

                    _cmd_allowed_tools = False
                    if cmd_name is not None:
                        resolved = ctx.resolve_at_command(cmd_name, remaining)
                        if resolved is None:
                            _pe(
                                f"Unknown command: @{cmd_name}. Available: {', '.join(ctx.discover_at_commands())}"
                            )
                            continue
                        _pi(f"@{resolved.name}: {resolved.description}")
                        # Expand any nested $skill / @command refs in the body
                        blocks.append(ctx.expand_inline_references(resolved.body))
                        if resolved.meta.tools_enabled:
                            _cmd_allowed_tools = True
                    elif remaining:
                        # Expand any inline $skill / @command / *@command refs
                        blocks.append(ctx.expand_inline_references(remaining))

                    user_input = "\n\n---\n\n".join(blocks)

                    if _cmd_allowed_tools:
                        loop_kwargs.pop("tool_choice", None)

                # Rebuild loop_kwargs in case tools were toggled
                if not ctx.tools_enabled and "tool_choice" not in loop_kwargs:
                    loop_kwargs["tool_choice"] = ToolChoice.none()
                elif ctx.tools_enabled:
                    loop_kwargs.pop("tool_choice", None)

                # Tips
                if _tip_scheduler is not None:
                    _tip_scheduler.record_message()
                    tip = _tip_scheduler.get_tip()
                    if tip:
                        console.print(f"[dim italic]{tip}[/]")

                # Chat message: run in background so prompt stays responsive.
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
            if browser_bridge_client is not None:
                with contextlib.suppress(Exception):
                    await browser_bridge_client.close()
            if supervisor_task is not None:
                if supervisor is not None:
                    with contextlib.suppress(Exception):
                        await supervisor.stop()
                if not supervisor_task.done():
                    supervisor_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await supervisor_task
            if daemon_task is not None:
                if not daemon_task.done():
                    daemon_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await daemon_task
                dc = getattr(daemon_task, "_daemon_client", None)
                if dc is not None:
                    with contextlib.suppress(Exception):
                        await dc.__aexit__(None, None, None)
            if background_tasks:
                await asyncio.gather(*background_tasks, return_exceptions=True)
            await ctx.stop_runtime()
            if _uds_inbox is not None:
                with contextlib.suppress(Exception):
                    await _uds_inbox.stop()
            try:
                dlog.session_event("end", session_id=ctx.session_id)
                dlog.flush()
                dlog.close()
            except Exception:
                _log.debug("suppressed exception in repl", exc_info=True)
            if _kairos_engine is not None and not _kairos_hooks_registered:
                with contextlib.suppress(Exception):
                    await _kairos_engine.stop()
            try:
                await run_cleanup()
            except Exception:
                _log.debug("suppressed exception in repl", exc_info=True)
            try:
                get_attribution_tracker().save()
            except Exception:
                _log.debug("suppressed exception in repl", exc_info=True)
            try:
                sess = await store.get_session(sid)
                if sess is not None and sess.status == SessionStatus.RUNNING:
                    await store.update_status(sid, SessionStatus.COMPLETED)
            except Exception:
                _log.debug("suppressed exception in repl", exc_info=True)
            store.close()
