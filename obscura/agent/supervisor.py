"""AgentSupervisor — daemon process that keeps configured agents alive.

Reads an agent manifest (YAML), starts each agent, and restarts on
failure with exponential backoff.  Wires the :class:`InteractionBus`
to a :class:`NativeNotifier` so important moments surface as macOS
popup notifications.

Usage::

    from obscura.agent.supervisor import AgentSupervisor

    supervisor = AgentSupervisor(
        config_path=Path("~/.obscura/agents.yaml"),
        user=authenticated_user,
    )
    await supervisor.run_forever()

Config format (``~/.obscura/agents.yaml``)::

    agents:
      - name: researcher
        type: loop          # "loop" | "daemon" | "aper"
        model: claude
        system_prompt: "You are a research assistant."
        max_turns: 25
        mcp_servers: auto   # auto-select based on task, or list of names

      - name: health-monitor
        type: daemon
        model: copilot
        triggers:
          - schedule: "*/5 * * * *"
            prompt: "Check system health and report anomalies"
            notify_user: true
            priority: high
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TYPE_CHECKING

from obscura.agent.interaction import (
    AttentionPriority,
    AttentionRequest,
    InteractionBus,
)

if TYPE_CHECKING:
    from obscura.auth.models import AuthenticatedUser

__all__ = ["AgentSupervisor", "SupervisorConfig", "AgentDefinition"]

logger = logging.getLogger(__name__)

_MAX_BACKOFF = 60.0  # seconds


# ---------------------------------------------------------------------------
# Config types
# ---------------------------------------------------------------------------


@dataclass
class TriggerDefinition:
    """Trigger definition from YAML config."""

    schedule: str = ""
    prompt: str = ""
    description: str = ""
    notify_user: bool = False
    priority: str = "normal"  # "low" | "normal" | "high" | "critical"


@dataclass
class AgentDefinition:
    """Agent definition from YAML config."""

    name: str = "agent"
    type: str = "loop"  # "loop" | "daemon" | "aper"
    model: str = "copilot"
    system_prompt: str = ""
    max_turns: int = 25
    mcp_servers: str | list[str] = "auto"
    triggers: list[TriggerDefinition] = field(
        default_factory=lambda: list[TriggerDefinition]()
    )
    tags: list[str] = field(default_factory=lambda: list[str]())


@dataclass
class SupervisorConfig:
    """Top-level supervisor config."""

    agents: list[AgentDefinition] = field(
        default_factory=lambda: list[AgentDefinition]()
    )

    @classmethod
    def from_yaml(cls, path: Path) -> SupervisorConfig:
        """Load config from a YAML file.

        Falls back gracefully if PyYAML isn't installed or file is missing.
        """
        try:
            import yaml  # type: ignore[import-untyped]
        except ImportError:
            logger.warning("PyYAML not installed — cannot load %s", path)
            return cls()

        resolved = path.expanduser()
        if not resolved.exists():
            logger.warning("Supervisor config not found: %s", resolved)
            return cls()

        with resolved.open() as f:
            raw: dict[str, Any] = yaml.safe_load(f) or {}

        agents: list[AgentDefinition] = []
        raw_agents: list[dict[str, Any]] = raw.get("agents", [])
        for entry in raw_agents:
            raw_triggers: list[dict[str, Any]] = entry.pop("triggers", [])
            triggers = [TriggerDefinition(**t) for t in raw_triggers]
            agents.append(AgentDefinition(**entry, triggers=triggers))

        return cls(agents=agents)


# ---------------------------------------------------------------------------
# Supervisor
# ---------------------------------------------------------------------------


class AgentSupervisor:
    """Daemon that keeps configured agents alive with restart-on-failure.

    Lifecycle:

    1. Parse ``agents.yaml`` config.
    2. Create an :class:`AgentRuntime` with a shared :class:`InteractionBus`.
    3. Wire a :class:`NativeNotifier` to the bus for macOS popups.
    4. For each agent definition, spawn and supervise in a background task.
    5. Block forever (``asyncio.Event().wait()``).
    """

    def __init__(
        self,
        config_path: Path,
        user: AuthenticatedUser,
        *,
        interaction_bus: InteractionBus | None = None,
    ) -> None:
        self._config_path = config_path
        self._user = user
        self._bus = interaction_bus or InteractionBus()
        self._stopped = False
        self._tasks: list[asyncio.Task[None]] = []

    @property
    def interaction_bus(self) -> InteractionBus:
        return self._bus

    async def run_forever(self) -> None:
        """Start all agents and block until stopped."""
        from obscura.agent.agents import AgentRuntime
        from obscura.notifications.native import NativeNotifier

        config = SupervisorConfig.from_yaml(self._config_path)
        if not config.agents:
            logger.warning("No agents defined in %s", self._config_path)
            return

        runtime = AgentRuntime(
            user=self._user,
            interaction_bus=self._bus,
        )
        await runtime.start()

        # Wire native notifications
        notifier = NativeNotifier()

        async def _on_attention(request: AttentionRequest) -> None:
            await notifier.attention(
                title=request.agent_name,
                message=request.message,
                priority=request.priority,
                actions=list(request.actions),
            )

        self._bus.on_attention(_on_attention)

        logger.info(
            "Supervisor starting %d agent(s) from %s",
            len(config.agents),
            self._config_path,
        )

        for agent_def in config.agents:
            task = asyncio.create_task(
                self._supervise(runtime, agent_def),
                name=f"supervisor:{agent_def.name}",
            )
            self._tasks.append(task)

        try:
            await asyncio.Event().wait()  # block forever
        except asyncio.CancelledError:
            pass
        finally:
            self._stopped = True
            for task in self._tasks:
                task.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)
            await runtime.stop()
            logger.info("Supervisor shut down.")

    async def stop(self) -> None:
        """Signal the supervisor to shut down."""
        self._stopped = True
        for task in self._tasks:
            task.cancel()

    async def _supervise(
        self,
        runtime: Any,
        agent_def: AgentDefinition,
    ) -> None:
        """Run a single agent with restart-on-failure and backoff."""
        backoff = 1.0

        while not self._stopped:
            try:
                logger.info(
                    "[supervisor] starting agent '%s' (type=%s, model=%s)",
                    agent_def.name,
                    agent_def.type,
                    agent_def.model,
                )
                await self._run_agent(runtime, agent_def)
                # Clean exit — no restart needed
                logger.info("[supervisor] agent '%s' exited cleanly", agent_def.name)
                backoff = 1.0
                break

            except asyncio.CancelledError:
                logger.info("[supervisor] agent '%s' cancelled", agent_def.name)
                return

            except Exception:
                logger.exception(
                    "[supervisor] agent '%s' crashed, restarting in %.1fs",
                    agent_def.name,
                    backoff,
                )
                await asyncio.sleep(min(backoff, _MAX_BACKOFF))
                backoff *= 2

    async def _run_agent(
        self,
        runtime: Any,
        agent_def: AgentDefinition,
    ) -> None:
        """Instantiate and run an agent based on its type."""
        from obscura.core.client import ObscuraClient

        client = ObscuraClient(
            agent_def.model,
            system_prompt=agent_def.system_prompt,
            user=self._user,
        )
        await client.start()

        try:
            if agent_def.type == "loop":
                await self._run_loop_agent(client, agent_def)
            elif agent_def.type == "daemon":
                await self._run_daemon_agent(client, agent_def)
            elif agent_def.type == "aper":
                await self._run_aper_agent(client, agent_def)
            else:
                logger.warning(
                    "Unknown agent type '%s' for '%s' — skipping",
                    agent_def.type,
                    agent_def.name,
                )
        finally:
            await client.stop()

    async def _run_loop_agent(
        self,
        client: Any,
        agent_def: AgentDefinition,
    ) -> None:
        from obscura.agent.loop_agent import LoopAgent

        agent = LoopAgent(
            client,
            name=agent_def.name,
            interaction_bus=self._bus,
            max_turns_per_input=agent_def.max_turns,
        )
        await agent.run_forever()

    async def _run_daemon_agent(
        self,
        client: Any,
        agent_def: AgentDefinition,
    ) -> None:
        from obscura.agent.daemon_agent import (
            DaemonAgent,
            ScheduleTrigger,
            Trigger,
        )

        triggers: list[Trigger] = []
        for tdef in agent_def.triggers:
            priority = _parse_priority(tdef.priority)
            if tdef.schedule:
                triggers.append(
                    ScheduleTrigger(
                        cron=tdef.schedule,
                        prompt=tdef.prompt,
                        description=tdef.description,
                        notify_user=tdef.notify_user,
                        priority=priority,
                    )
                )
            else:
                triggers.append(
                    Trigger(
                        kind="manual",
                        prompt=tdef.prompt,
                        description=tdef.description,
                        notify_user=tdef.notify_user,
                        priority=priority,
                    )
                )

        agent = DaemonAgent(
            client,
            name=agent_def.name,
            triggers=triggers,
            interaction_bus=self._bus,
            max_turns_per_trigger=agent_def.max_turns,
        )
        await agent.run_forever()

    async def _run_aper_agent(
        self,
        client: Any,
        agent_def: AgentDefinition,
    ) -> None:
        from obscura.agent.aper_loop_agent import APERLoopAgent

        agent = APERLoopAgent(
            client,
            name=agent_def.name,
            interaction_bus=self._bus,
            max_turns_per_input=agent_def.max_turns,
        )
        await agent.run_forever()


def _parse_priority(s: str) -> AttentionPriority:
    """Convert a string priority to :class:`AttentionPriority`."""
    mapping: dict[str, AttentionPriority] = {
        "low": AttentionPriority.LOW,
        "normal": AttentionPriority.NORMAL,
        "high": AttentionPriority.HIGH,
        "critical": AttentionPriority.CRITICAL,
    }
    return mapping.get(s.lower(), AttentionPriority.NORMAL)
