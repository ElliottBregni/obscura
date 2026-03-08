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
    imessage: dict[str, Any] | None = None  # {"contacts": [...], "poll_interval": 30}


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

    # Delegation
    can_delegate: bool = False
    delegate_allowlist: list[str] = field(default_factory=lambda: list[str]())
    max_delegation_depth: int = 3

    # Tool allowlist (None = all tools allowed)
    tool_allowlist: list[str] | None = None


@dataclass
class SupervisorConfig:
    """Top-level supervisor config."""

    agents: list[AgentDefinition] = field(
        default_factory=lambda: list[AgentDefinition]()
    )

    @classmethod
    def from_directory(cls, directory: Path) -> SupervisorConfig:
        """Scan a directory for ``*.agent.md`` files and build config.

        Each agent manifest is converted to an :class:`AgentDefinition`.
        """
        from obscura.manifest.loader import ManifestLoader

        resolved = directory.expanduser()
        if not resolved.is_dir():
            logger.warning("Manifest directory not found: %s", resolved)
            return cls()

        loader = ManifestLoader()
        manifests = loader.load_agent_manifests(resolved)
        if not manifests:
            logger.warning("No *.agent.md files found in %s", resolved)
            return cls()

        agents: list[AgentDefinition] = []
        for m in manifests:
            agents.append(AgentDefinition(
                name=m.name,
                type=m.agent_type,
                model=m.model,
                system_prompt=m.system_prompt,
                max_turns=m.max_turns,
                mcp_servers=m.mcp_servers,
                tags=list(m.tags),
                can_delegate=m.can_delegate,
                delegate_allowlist=list(m.delegate_allowlist),
                max_delegation_depth=m.max_delegation_depth,
                tool_allowlist=list(m.tool_allowlist) if m.tool_allowlist is not None else None,
            ))

        return cls(agents=agents)

    @classmethod
    def from_yaml(cls, path: Path) -> SupervisorConfig:
        """Load config from a YAML file.

        Falls back gracefully if PyYAML isn't installed or file is missing.
        """
        try:
            import yaml
        except ImportError:
            logger.warning("PyYAML not installed — cannot load %s", path)
            return cls()

        resolved = path.expanduser()
        if not resolved.exists():
            logger.warning("Supervisor config not found: %s", resolved)
            return cls()

        with resolved.open() as f:
            raw: dict[str, Any] = yaml.safe_load(f) or {}

        # Known fields for AgentDefinition / TriggerDefinition
        import dataclasses as _dc
        _agent_fields = {f.name for f in _dc.fields(AgentDefinition)}
        _trigger_fields = {f.name for f in _dc.fields(TriggerDefinition)}

        agents: list[AgentDefinition] = []
        raw_agents: list[dict[str, Any]] = raw.get("agents", [])
        for entry in raw_agents:
            raw_triggers: list[dict[str, Any]] = entry.pop("triggers", [])
            triggers = [
                TriggerDefinition(**{k: v for k, v in t.items() if k in _trigger_fields})
                for t in raw_triggers
            ]
            filtered = {k: v for k, v in entry.items() if k in _agent_fields}
            agents.append(AgentDefinition(**filtered, triggers=triggers))

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
        base_url: str = "http://localhost:8080",
    ) -> None:
        self._config_path = config_path
        self._user = user
        self._bus = interaction_bus or InteractionBus()
        self._stopped = False
        self._tasks: list[asyncio.Task[None]] = []
        self._base_url = base_url
        self._agent_cards: dict[str, Any] = {}

    @property
    def interaction_bus(self) -> InteractionBus:
        return self._bus

    @property
    def agent_cards(self) -> dict[str, Any]:
        """All generated A2A agent cards, keyed by agent name."""
        return self._agent_cards

    def get_agent_card(self, name: str) -> Any | None:
        """Retrieve the A2A agent card for a given agent name."""
        return self._agent_cards.get(name)

    async def run_forever(self) -> None:
        """Start all agents and block until stopped."""
        from obscura.agent.agents import AgentRuntime
        from obscura.notifications.native import NativeNotifier

        # Support both YAML and directory-based config
        if self._config_path.is_dir():
            config = SupervisorConfig.from_directory(self._config_path)
        else:
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

        # Generate A2A card for this agent
        self._generate_agent_card(agent_def)

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
            IMessageTrigger,
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
            elif tdef.imessage is not None:
                im_cfg = tdef.imessage
                triggers.append(
                    IMessageTrigger(
                        contacts=tuple(im_cfg.get("contacts", [])),
                        poll_interval=im_cfg.get("poll_interval", 30),
                        prompt=tdef.prompt,
                        description=tdef.description or "iMessage polling",
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


    def _generate_agent_card(self, agent_def: AgentDefinition) -> None:
        """Generate and store an A2A agent card for the given definition."""
        try:
            from obscura.integrations.a2a.agent_card import AgentCardGenerator

            card = AgentCardGenerator.from_agent_config(
                agent_name=agent_def.name,
                base_url=self._base_url,
                description=agent_def.system_prompt[:200] if agent_def.system_prompt else "",
                streaming=True,
            )
            self._agent_cards[agent_def.name] = card
            logger.debug("Generated A2A card for agent '%s'", agent_def.name)
        except Exception:
            logger.debug(
                "Could not generate A2A card for '%s'",
                agent_def.name,
                exc_info=True,
            )


def _parse_priority(s: str) -> AttentionPriority:
    """Convert a string priority to :class:`AttentionPriority`."""
    mapping: dict[str, AttentionPriority] = {
        "low": AttentionPriority.LOW,
        "normal": AttentionPriority.NORMAL,
        "high": AttentionPriority.HIGH,
        "critical": AttentionPriority.CRITICAL,
    }
    return mapping.get(s.lower(), AttentionPriority.NORMAL)
