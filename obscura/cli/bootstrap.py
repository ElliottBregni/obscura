"""obscura.cli.bootstrap — MCP/agent discovery and iMessage daemon lifecycle.

Canonical home for:
  - MCP server discovery
  - Agent discovery (lazy, metadata-only)
  - Inline ``@agent`` mention parsing and execution
  - iMessage daemon lifecycle
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from obscura.cli.render import console, print_warning
from obscura.core.client import ObscuraClient

if TYPE_CHECKING:
    from obscura.cli.commands import REPLContext


def _discover_mcp() -> tuple[list[dict[str, Any]], list[str]]:
    """Auto-discover MCP servers from ~/.obscura/mcp/. Returns (configs, names)."""
    try:
        from obscura.integrations.mcp.config_loader import (
            build_runtime_server_configs,
            discover_mcp_servers,
        )

        discovered = discover_mcp_servers()
        if discovered:
            return build_runtime_server_configs(discovered), [
                s.name for s in discovered
            ]
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
            AgentInfo(
                name=name,
                type=cfg.get("type", "loop"),
                model=cfg.get("model", "default"),
            )
            for name, cfg in agents.items()
        ]
    except Exception:
        return []


_INLINE_AGENT_MENTION_RE = re.compile(
    r"^\s*@(?P<name>[A-Za-z0-9][A-Za-z0-9_-]*)\s+(?P<prompt>.+?)\s*$",
    re.DOTALL,
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
    from obscura.cli.render import LabeledStreamRenderer
    from obscura.manifest.models import AgentManifest

    manifest: AgentManifest | None = None
    try:
        from obscura.tools.swarm import load_agent_configs  # noqa: PLC0415

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
                tools=list(cfg.get("tools", []))
                if isinstance(cfg.get("tools"), list)
                else [],
                tags=list(cfg.get("tags", []))
                if isinstance(cfg.get("tags"), list)
                else [],
                mcp_servers=parsed_mcp,
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
        renderer.finish()
        console.print("[dim][interrupted][/]")
    except Exception as exc:
        renderer.finish()
        from obscura.cli.render import print_error

        print_error(f"Inline @{agent_name} failed: {exc}")
    else:
        renderer.finish()
    finally:
        with contextlib.suppress(Exception):
            await agent.stop()
    console.print()
    return "".join(output_chunks).strip()


async def _start_imessage_daemon(client: Any) -> asyncio.Task[None] | None:
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
        # Load persisted schedules from ~/.obscura/schedules.json
        try:
            from obscura.agent.daemon_agent import ScheduleTrigger as _ST

            _schedules_path = Path.home() / ".obscura" / "schedules.json"
            if _schedules_path.is_file():
                import json as _json

                for sched in _json.loads(_schedules_path.read_text(encoding="utf-8")):
                    triggers.append(
                        _ST(
                            cron=sched["cron"],
                            prompt=sched["prompt"],
                            description=f"{sched.get('id', '?')}: {sched['prompt'][:40]}",
                            notify_user=bool(sched.get("notify", True)),
                        ),
                    )
        except Exception as _sched_exc:
            import logging as _sched_log

            _sched_log.getLogger(__name__).debug(
                "Failed to load persisted schedules: %s", _sched_exc,
            )

        daemon = DaemonAgent(daemon_client, name=agent_def.name, triggers=triggers)
        daemon._bus = bus  # type: ignore[attr-defined]
        task: asyncio.Task[None] = asyncio.create_task(
            daemon.loop_forever(),
            name=f"daemon-{agent_def.name}",  # type: ignore[arg-type]
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
