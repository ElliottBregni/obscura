"""obscura.cli._daemon — iMessage daemon lifecycle management.

Extracted from ``obscura/cli/__init__.py``.

Public API
----------
start_imessage_daemon(client) -> asyncio.Task | None
    Start the iMessage daemon if configured in agents.yaml.
    Returns the running asyncio.Task, or None when no daemon is configured.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from obscura.agent.daemon_agent import DaemonAgent, IMessageTrigger, ScheduleTrigger
from obscura.agent.interaction import AttentionPriority, InteractionBus
from obscura.agent.supervisor import SupervisorConfig
from obscura.cli.render import console as _console

_log = logging.getLogger("obscura.cli")


async def start_imessage_daemon(
    client: Any,
) -> asyncio.Task[None] | None:
    """Start iMessage daemon if configured in agents.yaml. Returns the task."""
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

        triggers: list[Any] = []
        for tdef in im_triggers:
            im_cfg = tdef.imessage or {}
            im_data = {
                k: v
                for k, v in im_cfg.items()
                if k not in {"contacts", "poll_interval"}
            }
            _priority_val = tdef.priority
            _priority = (
                _priority_val
                if isinstance(_priority_val, AttentionPriority)
                else AttentionPriority.NORMAL
            )
            triggers.append(
                IMessageTrigger(
                    contacts=tuple(im_cfg.get("contacts", [])),
                    poll_interval=im_cfg.get("poll_interval", 30),
                    notify_user=tdef.notify_user,
                    priority=_priority,
                    data=im_data,
                ),
            )

        bus = InteractionBus()

        async def _on_output(output: Any) -> None:
            from obscura.cli.render import push_notification
            from obscura.cli.renderer.channels import from_agent_output

            if not push_notification(from_agent_output(output)):
                text = getattr(output, "text", str(output))
                source = getattr(output, "source", agent_def.name)
                _console.print(f"[dim]\\[{source}][/] {text}")

        bus.on_output(_on_output)

        # Suppress daemon startup logs -- the bottom toolbar shows daemon
        # status, so raw StreamHandler output would corrupt the prompt UI.
        import logging as _logging

        _logging.getLogger("obscura.agent.daemon_agent").setLevel(_logging.WARNING)

        # Create a SEPARATE session for the daemon so it does not contend
        # with the REPL session for backend access. Migrated from direct
        # ObscuraClient construction to composition.build_core_session;
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
                import json as _sched_json

                for sched in _sched_json.loads(
                    _schedules_path.read_text(encoding="utf-8"),
                ):
                    triggers.append(
                        ScheduleTrigger(
                            cron=sched["cron"],
                            prompt=sched["prompt"],
                            description=f"{sched.get('id', '?')}: {sched['prompt'][:40]}",
                            notify_user=bool(sched.get("notify", True)),
                        ),
                    )
        except Exception as _sched_exc:
            _log.debug("Failed to load persisted schedules: %s", _sched_exc)

        daemon = DaemonAgent(daemon_client, name=agent_def.name, triggers=triggers)
        daemon._bus = bus  # pyright: ignore[reportPrivateUsage]
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
        # Stash client on the task so we can close it later
        task._daemon_client = daemon_client  # type: ignore[attr-defined]
        return task

    return None
