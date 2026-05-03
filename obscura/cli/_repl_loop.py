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
import time
import uuid
from pathlib import Path
from typing import Any

from obscura.core.client import ObscuraClient
from obscura.core.event_store import SQLiteEventStore, SessionStatus
from obscura.core.paths import resolve_obscura_home
from obscura.core.types import AgentEventKind, Backend, SessionRef, ToolChoice

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
    from obscura.cli._env_loader import bootstrap_env
    from obscura.cli._send import _session_state, send_message
    from obscura.cli.bootstrap import (
        _discover_agent_infos,  # pyright: ignore[reportPrivateUsage]
        _discover_mcp,  # pyright: ignore[reportPrivateUsage]
    )
    from obscura.cli.commands import (
        COMPLETIONS,
        REPLContext,
        handle_command,
    )
    from obscura.cli.prompt import (
        PromptStatus,
        StreamingStatus,
        _get_git_branch,  # pyright: ignore[reportPrivateUsage]
        animate_spinner,
        bordered_prompt,
        create_prompt_session,
    )
    from obscura.cli.render import (
        console,
        print_banner,
        print_ok,
        print_warning,
    )
    from obscura.cli.vector_memory_bridge import (
        init_vector_store,
        load_startup_memories,
        run_startup_maintenance,
    )

    # Event store
    db_path = resolve_obscura_home() / "events.db"
    store = SQLiteEventStore(db_path)
    sid = session_id or uuid.uuid4().hex

    # Resolve backend/model names from arguments or environment defaults
    import os

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

    # Run decay maintenance in background
    if vector_store is not None:
        run_startup_maintenance(vector_store)

    # Initialize memory channel router
    context_router = None
    turn_classifier = None
    if vector_store is not None:
        try:
            from obscura.memory_channels import (
                ContextRouter,
                TurnClassifier,
                load_channels_from_config,
            )

            _channels = load_channels_from_config()
            if _channels:
                context_router = ContextRouter(_channels, vector_store)
                turn_classifier = TurnClassifier(_channels)
        except Exception:
            pass

    # Compose system prompt
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

    # Inject user identity & preferences
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

    # Inject memory channel documentation
    if context_router is not None:
        try:
            from obscura.tools.memory_tools import build_channels_prompt_section

            channels_doc = build_channels_prompt_section(context_router.channels)
            if channels_doc:
                custom_sections.append(channels_doc)

            sys_channel_ctx = context_router.get_system_channels()
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
        pass

    # Inject KAIROS context
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

    # Inject coordinator system prompt
    try:
        from obscura.agent.coordinator import (
            get_coordinator_system_prompt,
            is_coordinator_mode,
        )

        if is_coordinator_mode():
            custom_sections.append(get_coordinator_system_prompt())
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

    # Gather system tools BEFORE client starts
    system_tools: list[Any] = []
    if tools_enabled:
        try:
            from obscura.tools.system import get_system_tool_specs

            system_tools = get_system_tool_specs()
        except Exception:
            pass

        if vector_store is not None:
            try:
                from obscura.tools.memory_tools import make_memory_tool_specs

                system_tools.extend(make_memory_tool_specs(cli_user))
            except Exception:
                pass

        for _getter, _mod in [
            ("get_worktree_tool_specs", "obscura.tools.worktree"),
            ("get_task_tool_specs", "obscura.tools.task_tools"),
            ("get_goal_tool_specs", "obscura.tools.goal_tools"),
            ("get_profile_tool_specs", "obscura.tools.profile_tools"),
            ("get_lsp_tool_specs", "obscura.tools.lsp"),
            ("get_browser_tool_specs", "obscura.tools.browser"),
        ]:
            try:
                import importlib

                _m = importlib.import_module(_mod)
                system_tools.extend(getattr(_m, _getter)())
            except Exception:
                pass

        # Load builtin plugin tools
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

    # Backfill capability field
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

    # Filter tools by capability grants
    if tools_enabled and system_tools:
        try:
            from obscura.plugins.capabilities import resolve_allowed_tools_from_config

            _allowed = resolve_allowed_tools_from_config()
            if _allowed is not None:
                system_tools = [
                    t
                    for t in system_tools
                    if not getattr(t, "capability", "") or t.name in _allowed
                ]
        except Exception:
            pass

    tool_count = len(system_tools)

    # Wire the ask_user callback
    if tools_enabled:
        try:
            from obscura.tools.system import UI as _UI_for_ask

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

            _UI_for_ask.set_ask_user_callback(_ask_user_handler)
        except Exception:
            pass

    # Wire plan-mode callbacks
    if tools_enabled:
        try:
            from obscura.tools.system import Session as _Session_for_plan

            def _set_permission_mode(mode: str) -> None:
                ctx.permission_mode = mode  # type: ignore[name-defined]  # set later

            _Session_for_plan.set_permission_mode_callback(_set_permission_mode)

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

            _Session_for_plan.set_plan_approval_callback(_plan_approval_handler)
        except Exception:
            pass

    # Wire user_interact callback
    if tools_enabled:
        try:
            from obscura.tools.system import UI as _UI_for_interact

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

            _UI_for_interact.set_user_interact_callback(_user_interact_handler)
        except Exception:
            pass

    # Load project hooks
    project_hooks = None
    try:
        from obscura.core.settings import load_all_hooks

        _hook_registry = load_all_hooks()
        if _hook_registry.count > 0:
            project_hooks = _hook_registry
    except Exception:
        pass

    # Wire memory channel TOOL_CALL hook
    _tool_router_ref = None
    if context_router is not None:
        from obscura.core.hooks import HookRegistry
        from obscura.core.types import AgentEventKind as _AEK

        if project_hooks is None:
            project_hooks = HookRegistry()

        def _channel_tool_signal(event: Any) -> None:
            context_router.update_signals_from_event(event)
            if _tool_router_ref is not None and context_router.signals.file_paths:
                _tool_router_ref.set_file_context(
                    list(context_router.signals.file_paths),
                )

        project_hooks.add_after(_channel_tool_signal, _AEK.TOOL_CALL)

    # Wire Kairos tool-call hooks
    _kairos_engine: Any = None
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

    # Build client
    async with ObscuraClient(
        backend,
        model=model,
        system_prompt=combined_system,
        tools=system_tools or None,
        mcp_servers=mcp_configs or None,
        hooks=project_hooks,
    ) as client:
        # Wire eval-driven tool router
        if tools_enabled:
            try:
                from obscura.core.compiler.compiled import ToolRoutingConfig
                from obscura.core.tool_router import ToolRouter
                from obscura.core.tool_score_index import ToolScoreIndex
                from obscura.plugins.loader import (
                    PluginLoader,
                    _load_plugin_config_flag,  # pyright: ignore[reportPrivateUsage]
                )
                from obscura.plugins.models import PluginSpec
                from obscura.plugins.registries.capability_index import CapabilityIndex

                _routing_config = ToolRoutingConfig()
                _score_index = ToolScoreIndex()
                _cap_index = CapabilityIndex()
                _pl = PluginLoader()
                _all_pspecs: list[PluginSpec] = []
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
                from obscura.core.types import ToolRouterCapable

                _backend_ref = client._backend  # pyright: ignore[reportPrivateUsage]
                if isinstance(_backend_ref, ToolRouterCapable):
                    _backend_ref.set_tool_router(_router)
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
            vector_store=vector_store,
            context_router=context_router,
            turn_classifier=turn_classifier,
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

        agent_infos = _discover_agent_infos()
        available_agents = [a.name for a in agent_infos] or None

        # Best-effort browser bridge attach
        browser_bridge_client: Any = None
        browser_status: dict[str, Any] | None = None
        if tools_enabled:
            try:
                from obscura.integrations.browser.client import attach_if_running

                browser_bridge_client, browser_status = await attach_if_running(
                    client.register_tool,
                )
            except Exception:
                browser_bridge_client, browser_status = None, None
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

        # Start supervisor if --supervise and agents.yaml has agents
        supervisor_task: asyncio.Task[None] | None = None
        supervisor: Any = None
        if supervise and agent_infos:
            try:
                from obscura.agent.supervisor import AgentSupervisor
                from obscura.auth.models import AuthenticatedUser

                sup_user = AuthenticatedUser(
                    user_id=os.environ.get("USER", "local"),
                    email="cli@obscura.local",
                    roles=("operator",),
                    org_id="local",
                    token_type="user",
                    raw_token="",
                )
                agents_yaml = resolve_obscura_home() / "agents.yaml"
                supervisor = AgentSupervisor(
                    config_path=agents_yaml,
                    user=sup_user,
                )
                supervisor_task = asyncio.create_task(
                    supervisor.run_forever(),
                    name="supervisor",
                )
                print_ok(f"Supervisor started -- {len(agent_infos)} agent(s) launching")
            except Exception as exc:
                print_warning(f"Supervisor failed to start: {exc}")

        # Start iMessage daemon (only when supervisor is NOT running)
        from obscura.cli._daemon import start_imessage_daemon

        daemon_task: asyncio.Task[None] | None = None
        _daemon_client: Any = None
        if supervisor_task is None:
            try:
                daemon_task = await start_imessage_daemon(ctx.client)
            except Exception as exc:
                print_warning(f"iMessage daemon failed to start: {exc}")
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
            from obscura.cli.prompt import RunningAgentInfo

            prompt_status.mode = mm.current.value
            prompt_status.model = ctx.model or ""
            running: list[str] = []
            details: list[RunningAgentInfo] = []
            if ctx.runtime is not None:
                try:
                    from datetime import UTC, datetime

                    from obscura.agent.agents import AgentStatus as _AS

                    _active = {_AS.RUNNING, _AS.WAITING, _AS.PENDING}
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
                    pass
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
                    from obscura.agent.agents import AgentStatus as _AS2

                    _active2 = {_AS2.RUNNING, _AS2.WAITING, _AS2.PENDING}
                    task_count += sum(
                        1 for a in ctx.runtime.list_agents() if a.status in _active2
                    )
                except Exception:
                    pass
            if daemon_task is not None and not daemon_task.done():
                task_count += 1
            prompt_status.task_count = task_count
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

        spinner_task = asyncio.create_task(animate_spinner(ss))

        # --- KAIROS integration ---
        _kairos_engine = None
        _kairos_hooks_registered = False
        try:
            from obscura.kairos.engine import KairosEngine, is_kairos_enabled

            if is_kairos_enabled():
                _kairos_engine = KairosEngine()
                if supervisor is not None and hasattr(supervisor, "hooks"):
                    from obscura.kairos.supervisor_hooks import register_kairos_hooks

                    register_kairos_hooks(supervisor.hooks, _kairos_engine)
                    _kairos_hooks_registered = True
                else:
                    await _kairos_engine.start()
        except Exception as _e:
            _swallow("kairos_start", _e)

        if _kairos_engine is not None:
            try:
                _agent_loop = getattr(client, "_loop", None)
                if _agent_loop is not None:
                    _kairos_engine.register_agent_loop(_agent_loop)
            except Exception as _e:
                _swallow("kairos_loop_wire", _e)

        # Wire AgentLoop into Arbiter
        try:
            from obscura.arbiter.hooks import register_agent_loop as _reg_arbiter_loop

            _al = getattr(client, "_loop", None)
            if _al is not None:
                _reg_arbiter_loop(_al)
        except Exception as _e:
            _swallow("arbiter_loop_wire", _e)

        # --- Tips scheduler ---
        _tip_scheduler = None
        try:
            from obscura.cli.tips import TipScheduler

            _tip_scheduler = TipScheduler()
        except Exception as _e:
            _swallow("tips_init", _e)

        # --- Frustration detector ---
        _frustration_detector = None
        try:
            from obscura.kairos.engine import is_kairos_enabled as _kairos_enabled

            if _kairos_enabled():
                from obscura.kairos.frustration import FrustrationDetector

                _frustration_detector = FrustrationDetector()
        except Exception as _e:
            _swallow("frustration_init", _e)

        # --- Away summary tracker ---
        _away_tracker = None
        try:
            from obscura.kairos.engine import is_kairos_enabled as _kairos_enabled2

            if _kairos_enabled2():
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

            def _on_peer_message(msg: dict[str, Any]) -> None:
                sender = msg.get("from", "?")
                text = msg.get("text", "")
                console.print(f"\n[bold cyan]Message from {sender}:[/] {text}")

            await _uds_inbox.start(on_message=_on_peer_message)
        except Exception as _e:
            _swallow("uds_init", _e)
            _uds_inbox = None

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
                                daemon_task = await start_imessage_daemon(ctx.client)
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

                # Voice input
                if user_input == "__VOICE_RECORD__":
                    voice_enabled = getattr(ctx, "voice_enabled", False)
                    if not voice_enabled:
                        console.print("[dim]Voice mode is off. Enable with /voice on[/]")
                        continue
                    try:
                        from obscura.voice.session import VoiceSession

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
                        console.print(f"[red]Voice error: {voice_exc}[/]")
                        continue

                # KAIROS: log user message
                if _kairos_engine is not None and _kairos_engine.is_running:
                    with contextlib.suppress(Exception):
                        _kairos_engine.log_user_message(user_input)

                # Ultrathink keyword detection
                if "ultrathink" in user_input.lower() and not user_input.startswith("/"):
                    if getattr(ctx, "effort_level", "medium") != "max":
                        ctx.effort_level = "max"
                        try:
                            from obscura.cli.tui_effects import ultrathink_banner

                            ultrathink_banner()
                        except Exception:
                            console.print("[bold bright_magenta]⚡ ULTRATHINK activated[/]")

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
                        pass

                # Away summary
                if _away_tracker is not None:
                    try:
                        if _away_tracker.should_generate():
                            from obscura.kairos.away_summary import generate_away_summary

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

                # *eval -- benchmark a command/skill chain
                if user_input.startswith("*"):
                    import subprocess as _sp

                    from obscura.cli.render import print_error as _pe
                    from obscura.cli.render import print_info as _pi

                    def _snapshot_git() -> str | None:
                        try:
                            r = _sp.run(
                                ["git", "diff", "--name-only", "HEAD"],
                                capture_output=True, text=True, timeout=5,
                            )
                            u = _sp.run(
                                ["git", "ls-files", "--others", "--exclude-standard"],
                                capture_output=True, text=True, timeout=5,
                            )
                            files = (r.stdout.strip() + "\n" + u.stdout.strip()).strip()
                            return files or None
                        except Exception:
                            return None

                    def _revert_changes(before_files: str | None) -> list[str]:
                        try:
                            r = _sp.run(
                                ["git", "diff", "--name-only", "HEAD"],
                                capture_output=True, text=True, timeout=5,
                            )
                            u = _sp.run(
                                ["git", "ls-files", "--others", "--exclude-standard"],
                                capture_output=True, text=True, timeout=5,
                            )
                            after = set(
                                (r.stdout.strip() + "\n" + u.stdout.strip()).strip().splitlines()
                            )
                            before = set(before_files.splitlines() if before_files else [])
                            new_files = after - before
                            reverted: list[str] = []
                            for f in sorted(new_files):
                                if not f:
                                    continue
                                cr = _sp.run(
                                    ["git", "checkout", "HEAD", "--", f],
                                    capture_output=True, timeout=5,
                                )
                                if cr.returncode != 0:
                                    import os as _os2
                                    try:
                                        _os2.remove(f)
                                    except OSError:
                                        continue
                                reverted.append(f)
                            return reverted
                        except Exception:
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

                        _pi(f"Running eval suite for @{cmd_name}: {len(suite.cases)} test case(s)")
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
                                    "Preferred tools for this task: " + ", ".join(case.preferred_tools)
                                )

                            chain_input = "\n\n---\n\n".join(chain_blocks)
                            eval_kwargs = dict(loop_kwargs)
                            if resolved.meta.tools_enabled:
                                eval_kwargs.pop("tool_choice", None)

                            for run in range(suite.runs_per_case):
                                if suite.runs_per_case > 1:
                                    _pi(f"  Run {run + 1}/{suite.runs_per_case}")
                                response = await send_message(ctx, chain_input, eval_kwargs, streaming_status=ss)
                                ss.reset()
                                grading = ctx.build_grading_prompt(cmd_name, case.input_args, response, case.criteria)
                                _pi("  Grading...")
                                grade_response = await send_message(ctx, grading, loop_kwargs, streaming_status=ss)
                                ss.reset()
                                total_criteria += len(case.criteria)
                                pass_count = grade_response.upper().count("| PASS")
                                total_pass += pass_count
                                if pass_count < len(case.criteria):
                                    reverted = _revert_changes(_pre_files)
                                    if reverted:
                                        _pe(
                                            f"  Eval failed ({pass_count}/{len(case.criteria)}) "
                                            f"-- reverted {len(reverted)} file(s): " + ", ".join(reverted)
                                        )
                                    else:
                                        _pe(f"  Eval failed ({pass_count}/{len(case.criteria)}) -- no file changes to revert")

                        _pi(f"\n-- Eval complete: {total_pass}/{total_criteria} criteria passed")
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
                    response = await send_message(ctx, chain_input, eval_kwargs, streaming_status=ss)
                    ss.reset()

                    cmd_criteria = getattr(resolved.meta, "eval_criteria", None)
                    criteria = cmd_criteria or [
                        "Response is relevant to the command's purpose",
                        "Response follows the command's output format",
                        "Response is complete (not truncated or missing sections)",
                        "Response is accurate (no hallucinated information)",
                        "Response is actionable (provides specific, useful details)",
                    ]
                    pass_threshold = getattr(resolved.meta, "eval_pass_threshold", None) or len(criteria)
                    grading = ctx.build_grading_prompt(cmd_name, remaining, response, criteria)
                    _pi("Grading...")
                    grade_response = await send_message(ctx, grading, loop_kwargs, streaming_status=ss)
                    ss.reset()

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
                        eval_store = EvalResultStore()
                        asyncio.create_task(eval_store.save_run(summary))
                    except Exception:
                        pass

                    pass_count = grade_response.upper().count("| PASS")
                    total = len(criteria)
                    _pi(f"Score: {pass_count}/{total} (threshold: {pass_threshold}/{total})")
                    if pass_count < pass_threshold:
                        reverted = _revert_changes(_pre_files)
                        if reverted:
                            _pe(
                                f"Eval failed ({pass_count}/{total}) -- reverted {len(reverted)} file(s): "
                                + ", ".join(reverted)
                            )
                        else:
                            _pe(f"Eval failed ({pass_count}/{total}) -- no file changes to revert")
                    else:
                        _pi(f"Eval passed ({pass_count}/{total}) -- changes kept")
                    continue

                # $skill / @command / chained input
                if user_input.startswith(("$", "@")):
                    from obscura.cli.render import print_error as _pe
                    from obscura.cli.render import print_info as _pi

                    skill_names, cmd_name, remaining = ctx.parse_chained_input(user_input)
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
                        blocks.append(resolved.body)
                        if resolved.meta.tools_enabled:
                            _cmd_allowed_tools = True
                    elif remaining:
                        blocks.append(remaining)

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
                from obscura.core.deep_log import dlog

                dlog.session_event("end", session_id=ctx.session_id)
                dlog.flush()
                dlog.close()
            except Exception:
                pass
            if _kairos_engine is not None and not _kairos_hooks_registered:
                with contextlib.suppress(Exception):
                    await _kairos_engine.stop()
            try:
                from obscura.core.cleanup import run_cleanup

                await run_cleanup()
            except Exception:
                pass
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
