"""obscura.cli.bootstrap — Session & runtime wiring for the Obscura REPL.

Responsible for:
  - MCP server discovery
  - Agent discovery (lazy, metadata-only)
  - iMessage daemon lifecycle
  - The ``_repl`` async entry point that boots a client + drives the REPL loop
"""

from __future__ import annotations

import asyncio
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from obscura.cli.commands import COMPLETIONS, REPLContext, handle_command
from obscura.cli.prompt import (
    PromptStatus, StreamingStatus, _get_git_branch,
    animate_spinner, bordered_prompt, create_prompt_session,
)
from obscura.cli.render import console, print_banner, print_warning
from obscura.cli import trace as trace_mod
from obscura.cli.vector_memory_bridge import init_vector_store, load_startup_memories
from obscura.core.client import ObscuraClient
from obscura.core.event_store import SQLiteEventStore, SessionStatus
from obscura.core.paths import resolve_obscura_home
from obscura.core.types import Backend, SessionRef, ToolChoice


def _discover_mcp() -> tuple[list[dict[str, Any]], list[str]]:
    """Auto-discover MCP servers from ~/.obscura/mcp/. Returns (configs, names)."""
    try:
        from obscura.integrations.mcp.config_loader import (
            build_runtime_server_configs, discover_mcp_servers,
        )
        discovered = discover_mcp_servers()
        if discovered:
            return build_runtime_server_configs(discovered), [s.name for s in discovered]
    except Exception:
        pass
    return [], []


@dataclass
class AgentInfo:
    """Lightweight descriptor for a configured agent."""
    name: str
    type: str = "loop"
    model: str = "default"
    status: str = "configured"


def _discover_agents() -> list[str]:
    return [a.name for a in _discover_agent_infos()]


def _discover_agent_infos() -> list[AgentInfo]:
    try:
        from obscura.tools.swarm import load_agent_configs  # noqa: PLC0415
        agents = load_agent_configs(include_disabled=True)
        return [
            AgentInfo(name=name, type=cfg.get("type", "loop"), model=cfg.get("model", "default"))
            for name, cfg in agents.items()
        ]
    except Exception:
        return []


_INLINE_AGENT_MENTION_RE = re.compile(
    r"^\s*@(?P<name>[A-Za-z0-9][A-Za-z0-9_-]*)\s+(?P<prompt>.+?)\s*$", re.DOTALL,
)


def _parse_inline_agent_mention(text: str) -> tuple[str, str] | None:
    match = _INLINE_AGENT_MENTION_RE.match(text)
    if not match:
        return None
    agent_name = match.group("name").strip()
    prompt = match.group("prompt").strip()
    return (agent_name, prompt) if agent_name and prompt else None


async def _run_inline_agent_from_mention(ctx: REPLContext, text: str) -> str | None:
    parsed = _parse_inline_agent_mention(text)
    if parsed is None:
        return None
    agent_name, prompt = parsed
    runtime = await ctx.get_runtime()
    from obscura.manifest.models import AgentManifest
    from obscura.cli.render import LabeledStreamRenderer
    manifest: AgentManifest | None = None
    try:
        from obscura.tools.swarm import load_agent_configs  # noqa: PLC0415
        agent_configs = load_agent_configs(include_disabled=True)
        cfg = agent_configs.get(agent_name)
        if cfg is not None:
            if cfg.get("type") == "daemon":
                print_warning(f"@{agent_name} is a daemon agent and cannot be invoked inline.")
                return ""
            skills_cfg = cfg.get("skills", {})
            if not isinstance(skills_cfg, dict):
                skills_cfg = {}
            raw_mcp = cfg.get("mcp_servers", [])
            parsed_mcp: list[dict[str, Any]] = []
            if isinstance(raw_mcp, list):
                for server in raw_mcp:
                    if isinstance(server, dict):
                        parsed_mcp.append(server)
                    elif isinstance(server, str) and server.strip():
                        parsed_mcp.append({"name": server.strip()})
            manifest = AgentManifest(
                name=str(cfg.get("name", agent_name)),
                provider=str(cfg.get("provider") or cfg.get("model", ctx.backend)),
                system_prompt=str(cfg.get("system_prompt", "")),
                max_turns=int(cfg.get("max_turns", ctx.max_turns)),
                tools=list(cfg.get("tools", [])) if isinstance(cfg.get("tools"), list) else [],
                tags=list(cfg.get("tags", [])) if isinstance(cfg.get("tags"), list) else [],
                mcp_servers=parsed_mcp,
                skills_config=skills_cfg,
                can_delegate=bool(cfg.get("can_delegate", False)),
                delegate_allowlist=list(cfg.get("delegate_allowlist", []))
                    if isinstance(cfg.get("delegate_allowlist"), list) else [],
                max_delegation_depth=int(cfg.get("max_delegation_depth", 3)),
                tool_allowlist=list(cfg.get("tool_allowlist", []))
                    if isinstance(cfg.get("tool_allowlist"), list) else None,
            )
    except Exception as exc:
        print_warning(f"Failed loading @{agent_name} manifest: {exc}")
    if manifest is None:
        print_warning(f"No manifest found for @{agent_name}; running with SDK defaults.")
        agent = runtime.spawn(agent_name, model=ctx.backend, system_prompt="")
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
        from obscura.cli.render import print_error
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


async def _start_imessage_daemon(client: Any) -> asyncio.Task[None] | None:
    from obscura.agent.supervisor import SupervisorConfig
    from obscura.agent.daemon_agent import DaemonAgent
    from obscura.agent.interaction import InteractionBus
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
            im_data = {k: v for k, v in im_cfg.items() if k not in {"contacts", "poll_interval"}}
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
        import logging as _logging
        _logging.getLogger("obscura.agent.daemon_agent").setLevel(_logging.WARNING)
        daemon_client = ObscuraClient(agent_def.model, system_prompt=agent_def.system_prompt)
        await daemon_client.__aenter__()
        daemon = DaemonAgent(daemon_client, name=agent_def.name, triggers=triggers)
        daemon._bus = bus  # type: ignore[attr-defined]
        task: asyncio.Task[None] = asyncio.create_task(
            daemon.loop_forever(), name=f"daemon-{agent_def.name}"  # type: ignore[arg-type]
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
        task._daemon_client = daemon_client  # type: ignore[attr-defined]
        return task
    return None


def _wire_ask_user_callback() -> None:
    try:
        from obscura.tools.system import set_ask_user_callback
        async def _ask_user_handler(question: str, choices: list[str], allow_custom: bool = False) -> str:
            from obscura.cli.widgets import (
                ModelQuestionRequest, ask_model_question,
                confirm_attention, AttentionWidgetRequest,
            )
            if choices:
                result = await confirm_attention(AttentionWidgetRequest(
                    request_id="ask_user", agent_name="assistant",
                    message=question, priority="normal", actions=tuple(choices),
                ))
                return result.action
            result = await ask_model_question(ModelQuestionRequest(question=question))
            return result.text
        set_ask_user_callback(_ask_user_handler)
    except Exception:
        pass


def _wire_user_interact_callback() -> None:
    try:
        from obscura.tools.system import set_user_interact_callback
        async def _user_interact_handler(**kwargs: Any) -> dict[str, Any]:
            mode = kwargs.get("mode", "question")
            if mode == "permission":
                from obscura.cli.widgets import PermissionWidgetRequest, confirm_permission
                result = await confirm_permission(PermissionWidgetRequest(
                    action=kwargs.get("action", ""), reason=kwargs.get("reason", ""), risk=kwargs.get("risk", "low"),
                ))
                return {"approved": result.action == "approve"}
            elif mode == "notify":
                from obscura.cli.widgets import NotifyWidgetRequest, render_notification_banner
                render_notification_banner(NotifyWidgetRequest(
                    title=kwargs.get("title", ""), message=kwargs.get("message", ""),
                    priority=kwargs.get("priority", "normal"),
                ))
                return {}
            from obscura.cli.widgets import (
                ModelQuestionRequest, ask_model_question,
                confirm_attention, AttentionWidgetRequest,
            )
            choices = kwargs.get("choices", [])
            question = kwargs.get("question", "")
            if choices:
                result = await confirm_attention(AttentionWidgetRequest(
                    request_id="user_interact", agent_name="assistant",
                    message=question, priority="normal", actions=tuple(choices),
                ))
                return {"selected": result.action}
            result = await ask_model_question(ModelQuestionRequest(question=question))
            return {"selected": result.text}
        set_user_interact_callback(_user_interact_handler)
    except Exception:
        pass


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
    supervise: bool = False,
) -> None:
    """Core async loop — runs the interactive REPL or single-shot."""
    from obscura.cli.repl import send_message

    db_path = resolve_obscura_home() / "events.db"
    store = SQLiteEventStore(db_path)
    sid = session_id or uuid.uuid4().hex

    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:
        pass

    tools_enabled = tools == "on"
    mcp_configs: list[dict[str, Any]] = []
    mcp_names: list[str] = []
    if tools_enabled:
        mcp_configs, mcp_names = _discover_mcp()

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
    vector_store = init_vector_store(cli_user)

    from obscura.core.context import load_obscura_memory
    from obscura.core.system_prompts import compose_environment_context, compose_system_prompt

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

    if vector_store is not None:
        vm_startup = load_startup_memories(vector_store, sid, top_k=3)
        if vm_startup:
            custom_sections.append(vm_startup)
    try:
        from obscura.agent import AGENT_TYPE_REGISTRY
        from obscura.plugins.builtins import list_builtin_plugin_ids
        env_section = compose_environment_context(
            plugin_ids=list_builtin_plugin_ids(),
            capabilities=["shell.exec","file.read","file.write","git.ops","web.browse","search.web","security.scan"],
            agent_types=list(AGENT_TYPE_REGISTRY.keys()),
        )
        if env_section:
            custom_sections.append(env_section)
    except Exception:
        pass

    combined_system = compose_system_prompt(
        base=system, include_default=include_default, custom_sections=custom_sections or None,
    )

    system_tools: list[Any] = []
    skipped_tools: list[tuple[str, str]] = []
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
        try:
            from obscura.plugins.loader import get_all_builtin_tool_specs_with_report
            existing_names = {t.name for t in system_tools}
            resolved, skipped_tools = get_all_builtin_tool_specs_with_report()
            for tool in resolved:
                if tool.name not in existing_names:
                    system_tools.append(tool)
                    existing_names.add(tool.name)
        except Exception:
            pass
    tool_count = len(system_tools)

    if tools_enabled:
        _wire_ask_user_callback()
        _wire_user_interact_callback()

    project_hooks = None
    try:
        from obscura.core.settings import load_all_hooks
        _hook_registry = load_all_hooks()
        if _hook_registry.count > 0:
            project_hooks = _hook_registry
    except Exception:
        pass

    async with ObscuraClient(
        backend, model=model, system_prompt=combined_system,
        tools=system_tools or None, mcp_servers=mcp_configs or None, hooks=project_hooks,
    ) as client:
        if session_id:
            try:
                await client.resume_session(SessionRef(session_id=session_id, backend=Backend(backend)))
            except Exception as exc:
                print_warning(f"Resume failed for session {session_id[:12]}: {exc}. Starting fresh.")
                try:
                    await client.reset_session()
                except Exception:
                    pass

        loop_kwargs: dict[str, Any] = {}
        if not tools_enabled:
            loop_kwargs["tool_choice"] = ToolChoice.none()

        ctx = REPLContext(
            client=client, store=store, session_id=sid, backend=backend, model=model,
            system_prompt=combined_system, max_turns=max_turns, tools_enabled=tools_enabled,
            mcp_configs=mcp_configs, confirm_enabled=confirm, vector_store=vector_store,
        )

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

        mm = ctx.get_mode_manager()
        ss = StreamingStatus()
        agent_infos = _discover_agent_infos()
        available_agents = [a.name for a in agent_infos] or None

        health_checks: list[Any] = []
        try:
            from obscura.core.health import collect_startup_health
            health_checks = collect_startup_health(
                vector_store=vector_store, skipped_tools=skipped_tools if tools_enabled else None,
            )
        except Exception:
            pass

        print_banner(
            backend, model, sid, tool_count=tool_count, mcp_servers=mcp_names or None,
            mode=mm.current.value, available_agents=available_agents,
            agent_infos=agent_infos or None, health_checks=health_checks or None,
        )

        supervisor_task: asyncio.Task[None] | None = None
        _supervisor: Any = None
        if supervise and agent_infos:
            try:
                from obscura.agent.supervisor import AgentSupervisor
                from obscura.auth.models import AuthenticatedUser
                import os as _os
                sup_user = AuthenticatedUser(
                    user_id=_os.environ.get("USER", "local"), email="cli@obscura.local",
                    roles=("operator",), org_id="local", token_type="user", raw_token="",
                )
                _supervisor = AgentSupervisor(config_path=resolve_obscura_home()/"agents.yaml", user=sup_user)
                supervisor_task = asyncio.create_task(_supervisor.run_forever(), name="supervisor")
                ctx._supervisor = _supervisor
                ctx._supervisor_task = supervisor_task
                from obscura.cli.render import print_ok
                print_ok(f"Supervisor started — {len(agent_infos)} agent(s) launching")
            except Exception as exc:
                print_warning(f"Supervisor failed to start: {exc}")

        daemon_task: asyncio.Task[None] | None = None
        prompt_status = PromptStatus(model=model or "", branch=_get_git_branch(), session_id=sid, mode=mm.current.value)

        def _refresh_prompt_status() -> None:
            prompt_status.mode = mm.current.value
            prompt_status.model = ctx.model or ""
            running: list[str] = []
            if ctx._runtime is not None:
                try:
                    from obscura.agent.agents import AgentStatus as _AS
                    for a in ctx._runtime.list_agents(status=_AS.RUNNING):
                        running.append(a.config.name)
                except Exception:
                    pass
            if daemon_task is not None and not daemon_task.done():
                name = daemon_task.get_name()
                label = name.removeprefix("daemon-") if name.startswith("daemon-") else name
                if label not in running:
                    running.append(label)
            prompt_status.running_agents = running
            task_count = 0
            if ctx._runtime is not None:
                try:
                    from obscura.agent.agents import AgentStatus as _AS2
                    _active = {_AS2.RUNNING, _AS2.WAITING, _AS2.PENDING}
                    task_count += sum(1 for a in ctx._runtime.list_agents() if a.status in _active)
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

        session_obj = create_prompt_session(COMPLETIONS, streaming_status=ss, prompt_status=prompt_status)
        spinner_task = asyncio.create_task(animate_spinner(ss))
        background_tasks: set[asyncio.Task[str]] = set()

        try:
            while True:
                try:
                    _refresh_prompt_status()
                    user_input = await bordered_prompt(session_obj)
                except (EOFError, KeyboardInterrupt):
                    console.print()
                    break
                if not user_input:
                    continue
                if user_input.startswith("/"):
                    if not ctx.tools_enabled:
                        loop_kwargs["tool_choice"] = ToolChoice.none()
                    else:
                        loop_kwargs.pop("tool_choice", None)
                    result = await handle_command(user_input, ctx)
                    if result == "quit":
                        break
                    continue
                if not ctx.tools_enabled:
                    loop_kwargs["tool_choice"] = ToolChoice.none()
                else:
                    loop_kwargs.pop("tool_choice", None)
                task = asyncio.create_task(send_message(ctx, user_input, loop_kwargs, streaming_status=ss))
                background_tasks.add(task)
                def _on_done(t: asyncio.Task[str]) -> None:
                    background_tasks.discard(t)
                    ss.reset()
                task.add_done_callback(_on_done)
        finally:
            spinner_task.cancel()
            if supervisor_task is not None:
                if _supervisor is not None:
                    try:
                        await _supervisor.stop()
                    except Exception:
                        pass
                if not supervisor_task.done():
                    supervisor_task.cancel()
                    try:
                        await supervisor_task
                    except (asyncio.CancelledError, Exception):
                        pass
            if daemon_task is not None and not daemon_task.done():
                daemon_task.cancel()
                try:
                    await daemon_task
                except (asyncio.CancelledError, Exception):
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
