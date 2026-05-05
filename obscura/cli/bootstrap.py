"""obscura.cli.bootstrap — MCP/agent discovery and iMessage daemon lifecycle.

Canonical home for:
  - MCP server discovery
  - Agent discovery (lazy, metadata-only)
  - Inline ``!agent`` mention parsing and execution (formerly ``@agent``,
    moved to ``!`` so it stops conflicting with ``@command`` references)
  - iMessage daemon lifecycle
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from obscura.agent.daemon_agent import (
    DaemonAgent,
    IMessageTrigger,
    ScheduleTrigger,
)
from obscura.agent.interaction import AttentionPriority, InteractionBus
from obscura.agent.supervisor import SupervisorConfig
from obscura.cli.render import (
    LabeledStreamRenderer,
    console,
    print_error,
    print_warning,
)
from obscura.tools.swarm import load_agent_configs
from obscura.integrations.mcp.config_loader import (
    build_runtime_server_configs,
    discover_mcp_servers,
)
from obscura.manifest.models import AgentManifest

logger = logging.getLogger(__name__)


if TYPE_CHECKING:
    from obscura.cli.commands import REPLContext


def _discover_mcp() -> tuple[list[dict[str, Any]], list[str]]:  # pyright: ignore[reportUnusedFunction]
    """Auto-discover MCP servers from ~/.obscura/mcp/. Returns (configs, names)."""
    try:
        discovered = discover_mcp_servers()
        if discovered:
            return build_runtime_server_configs(discovered), [
                s.name for s in discovered
            ]
    except Exception:
        logger.debug("suppressed exception in _discover_mcp", exc_info=True)
    return [], []


@dataclass
class AgentInfo:
    """Lightweight descriptor for a configured agent."""

    name: str
    type: str = "loop"
    model: str = "default"
    status: str = "configured"


def _discover_agents() -> list[str]:  # pyright: ignore[reportUnusedFunction]
    return [a.name for a in _discover_agent_infos()]


def _discover_agent_infos() -> list[AgentInfo]:
    try:
        agents = load_agent_configs(include_disabled=True)
        return [
            AgentInfo(
                name=name,
                type=cfg.get("type", "loop"),
                model=cfg.get("model", "default"),
            )
            for name, cfg in agents.items()
        ]
    except Exception:
        logger.debug("suppressed exception in _discover_agent_infos", exc_info=True)
        return []


_INLINE_AGENT_MENTION_RE = re.compile(
    r"^\s*!(?P<name>[A-Za-z0-9][A-Za-z0-9_-]*)\s+(?P<prompt>.+?)\s*$",
    re.DOTALL,
)


def _parse_inline_agent_mention(text: str) -> tuple[str, str] | None:
    match = _INLINE_AGENT_MENTION_RE.match(text)
    if not match:
        return None
    agent_name = match.group("name").strip()
    prompt = match.group("prompt").strip()
    return (agent_name, prompt) if agent_name and prompt else None


async def _run_inline_agent_from_mention(  # pyright: ignore[reportUnusedFunction]
    ctx: REPLContext, text: str
) -> str | None:
    parsed = _parse_inline_agent_mention(text)
    if parsed is None:
        return None
    agent_name, prompt = parsed
    runtime = await ctx.get_runtime()

    manifest: AgentManifest | None = None
    try:
        agent_configs = load_agent_configs(include_disabled=True)
        cfg = agent_configs.get(agent_name)
        if cfg is not None:
            if cfg.get("type") == "daemon":
                print_warning(
                    f"@{agent_name} is a daemon agent and cannot be invoked inline.",
                )
                return ""
            skills_cfg = cfg.get("skills", {})
            if not isinstance(skills_cfg, dict):
                skills_cfg = {}
            raw_mcp = cfg.get("mcp_servers", [])
            parsed_mcp: list[dict[str, Any]] = []
            if isinstance(raw_mcp, list):
                for server in cast(list[Any], raw_mcp):
                    if isinstance(server, dict):
                        parsed_mcp.append(cast(dict[str, Any], server))
                    elif isinstance(server, str) and server.strip():
                        parsed_mcp.append({"name": server.strip()})
            raw_model_id = cfg.get("model_id")
            raw_params_val = cfg.get("completion_params")
            raw_params: dict[str, Any] = (
                cast(dict[str, Any], raw_params_val)
                if isinstance(raw_params_val, dict)
                else {}
            )
            raw_tools = cfg.get("tools")
            raw_tags = cfg.get("tags")
            raw_delegate = cfg.get("delegate_allowlist")
            raw_tool_allow = cfg.get("tool_allowlist")
            # AgentManifest has pydantic field aliases: ``provider`` is exposed
            # as ``model`` and ``mcp_servers`` as ``mcp_server_refs``.  Pyright
            # follows the alias only, even though ``populate_by_name=True``
            # accepts both at runtime — route through ``model_validate`` so the
            # dict bypasses constructor argument-name checking.
            manifest = AgentManifest.model_validate(
                {
                    "name": str(cfg.get("name", agent_name)),
                    "provider": str(cfg.get("provider") or ctx.backend),
                    "model_id": str(raw_model_id) if raw_model_id else None,
                    "completion_params": raw_params,
                    "system_prompt": str(cfg.get("system_prompt", "")),
                    "max_turns": int(cfg.get("max_turns", ctx.max_turns)),
                    "tools": list(cast(list[Any], raw_tools))
                    if isinstance(raw_tools, list)
                    else [],
                    "tags": list(cast(list[Any], raw_tags))
                    if isinstance(raw_tags, list)
                    else [],
                    "mcp_servers": parsed_mcp,
                    "skills_config": skills_cfg,
                    "can_delegate": bool(cfg.get("can_delegate", False)),
                    "delegate_allowlist": list(cast(list[Any], raw_delegate))
                    if isinstance(raw_delegate, list)
                    else [],
                    "max_delegation_depth": int(cfg.get("max_delegation_depth", 3)),
                    "tool_allowlist": list(cast(list[Any], raw_tool_allow))
                    if isinstance(raw_tool_allow, list)
                    else None,
                }
            )
    except Exception as exc:
        logger.debug(
            "suppressed exception in _run_inline_agent_from_mention", exc_info=True
        )
        print_warning(f"Failed loading @{agent_name} manifest: {exc}")
    if manifest is None:
        print_warning(
            f"No manifest found for @{agent_name}; running with SDK defaults.",
        )
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
        logger.debug(
            "suppressed exception in _run_inline_agent_from_mention", exc_info=True
        )
        renderer.finish()
        console.print("[dim][interrupted][/]")
    except Exception as exc:
        logger.debug(
            "suppressed exception in _run_inline_agent_from_mention", exc_info=True
        )
        renderer.finish()
        print_error(f"Inline @{agent_name} failed: {exc}")
    else:
        renderer.finish()
    finally:
        with contextlib.suppress(Exception):
            await agent.stop()
    console.print()
    return "".join(output_chunks).strip()


async def _start_imessage_daemon(  # pyright: ignore[reportUnusedFunction]
    client: Any,
) -> asyncio.Task[None] | None:
    _console = console

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

        triggers: list[Any] = []
        for tdef in im_triggers:
            im_cfg = tdef.imessage or {}
            im_data = {
                k: v
                for k, v in im_cfg.items()
                if k not in {"contacts", "poll_interval"}
            }
            priority_val = tdef.priority
            priority = (
                priority_val
                if isinstance(priority_val, AttentionPriority)
                else AttentionPriority.NORMAL
            )
            triggers.append(
                IMessageTrigger(
                    contacts=tuple(im_cfg.get("contacts", [])),
                    poll_interval=im_cfg.get("poll_interval", 30),
                    notify_user=tdef.notify_user,
                    priority=priority,
                    data=im_data,
                ),
            )
        bus = InteractionBus()

        async def _on_output(output: Any) -> None:
            # Route through the v2 notification channel when a renderer
            # is active; fall back to direct print otherwise (script /
            # headless contexts).
            from obscura.cli.render import push_notification
            from obscura.cli.renderer.channels import from_agent_output

            if not push_notification(from_agent_output(output)):
                text = getattr(output, "text", str(output))
                source = getattr(output, "source", agent_def.name)
                _console.print(f"[dim]\\[{source}][/] {text}")

        bus.on_output(_on_output)
        logging.getLogger("obscura.agent.daemon_agent").setLevel(logging.WARNING)
        # Migrated from direct ObscuraClient construction to composition;
        # session.run_loop_to_completion / .send / .reset_session quack
        # the same way DaemonAgent expects.
        from obscura.composition.core import build_core_session
        from obscura.composition.session import SessionConfig

        daemon_client = await build_core_session(
            SessionConfig(
                backend=agent_def.model,
                system_prompt=agent_def.system_prompt,
                inject_claude_context=False,
            ),
            surface="a2a",
        )
        # Load persisted schedules from ~/.obscura/schedules.json
        try:
            _schedules_path = Path.home() / ".obscura" / "schedules.json"
            if _schedules_path.is_file():
                for sched in json.loads(_schedules_path.read_text(encoding="utf-8")):
                    triggers.append(
                        ScheduleTrigger(
                            cron=sched["cron"],
                            prompt=sched["prompt"],
                            description=f"{sched.get('id', '?')}: {sched['prompt'][:40]}",
                            notify_user=bool(sched.get("notify", True)),
                        ),
                    )
        except Exception as _sched_exc:
            logging.getLogger(__name__).debug(
                "Failed to load persisted schedules: %s",
                _sched_exc,
            )

        daemon = DaemonAgent(daemon_client, name=agent_def.name, triggers=triggers)
        daemon._bus = bus  # type: ignore[attr-defined]
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
        return task
    return None
