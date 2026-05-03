"""obscura.agent.supervised_runtime — Glue layer between Supervisor and AgentRuntime.

Provides :class:`SupervisedRuntime`, a single entry point that:

1. Creates (or reuses) an :class:`~obscura.agent.agents.AgentRuntime`
2. Spawns the coordinator :class:`~obscura.agent.agents.Agent` and calls ``start()``
3. Builds an :class:`~obscura.core.agent_loop.AgentLoop` adaptor that delegates
   to the agent's ``stream_loop()``
4. Passes that adaptor to :class:`~obscura.core.supervisor.Supervisor`.run so the
   full Supervisor lifecycle wraps the run (lock → context freeze → model turn →
   memory commit → finalize)

Usage::

    from obscura.agent.supervised_runtime import SupervisedRuntime, SupervisedRuntimeConfig

    sr = SupervisedRuntime(
        db_path="/tmp/sessions.db",
        config=SupervisedRuntimeConfig(coordinator_model="claude"),
    )

    async for event in sr.run(session_id="sess-1", prompt="Refactor the auth module"):
        print(event.kind, event.payload)

    await sr.close()
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from obscura.agent.agents import AgentRuntime
from obscura.core.supervisor import Supervisor, SupervisorConfig
from obscura.core.supervisor.types import SupervisorEvent

if TYPE_CHECKING:
    from obscura.agent.agents import Agent
    from obscura.auth.models import AuthenticatedUser
    from obscura.core.types import AgentEvent

logger = logging.getLogger(__name__)


@dataclass
class SupervisedRuntimeConfig:
    """Configuration for :class:`SupervisedRuntime`.

    All fields are optional — sensible defaults are used for each.
    """

    # Coordinator agent settings
    coordinator_name: str = "coordinator"
    coordinator_model: str = "copilot"
    coordinator_system_prompt: str = ""
    coordinator_max_iterations: int = 10
    coordinator_timeout_seconds: float = 300.0

    # Whether to enable coordinator/swarm mode on the coordinator agent
    can_delegate: bool = True
    delegate_allowlist: list[str] = field(default_factory=list[str])

    # Memory namespace for the coordinator
    memory_namespace: str = "supervised"

    # Supervisor config overrides (None = defaults)
    supervisor_config: SupervisorConfig | None = None

    # If True, a fresh agent is spawned for every run() call.
    # If False, the same agent is reused across runs (faster, shares memory).
    fresh_agent_per_run: bool = False


class _AgentLoopAdaptor:
    """Adapts :meth:`Agent.stream_loop` to the ``agent_loop`` duck-type that
    :meth:`Supervisor.run` expects.

    The Supervisor only needs::

        async for event in agent_loop.run(prompt, session_id=...):
            ...

    This adaptor satisfies that contract by forwarding to
    ``agent.stream_loop(prompt)``.
    """

    def __init__(self, agent: Agent, max_turns: int | None = None) -> None:
        self._agent = agent
        self._max_turns = max_turns

    async def run(
        self,
        prompt: str,
        *,
        session_id: str = "",  # noqa: ARG002 — accepted for compat, not used
        **_kwargs: Any,
    ) -> AsyncIterator[AgentEvent]:
        """Delegate to agent.stream_loop and yield all AgentEvents."""
        async for event in self._agent.stream_loop(
            prompt,
            max_turns=self._max_turns,
        ):
            yield event


class SupervisedRuntime:
    """Single entry point that wires :class:`Supervisor` → :class:`AgentRuntime`.

    Manages the full lifecycle::

        acquire_lock → build_context → run_model ⇄ run_tools →
        commit_memory → finalize → release_lock

    while delegating model execution to a coordinator :class:`Agent` whose
    swarm tools (``spawn_agents``, ``spawn_subagent``, ``send_message``) allow
    it to fan out work to specialist sub-agents.
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        config: SupervisedRuntimeConfig | None = None,
        user: AuthenticatedUser | None = None,
    ) -> None:
        self._db_path = Path(db_path)
        self._config = config or SupervisedRuntimeConfig()
        self._user = user
        self._supervisor = Supervisor(
            db_path=self._db_path,
            config=self._config.supervisor_config,
        )
        self._runtime: AgentRuntime | None = None
        self._coordinator: Agent | None = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(
        self,
        session_id: str,
        prompt: str,
        *,
        system_prompt: str = "",
        context_instructions: str = "",
        session_history: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> AsyncIterator[SupervisorEvent]:
        """Execute a supervised, multi-agent run.

        Spawns (or reuses) the coordinator agent, wires it into the Supervisor,
        and yields :class:`SupervisorEvent` objects for every lifecycle step.

        Args:
            session_id: Stable identifier for the session.  Used by the
                Supervisor for lock management and event logging.
            prompt: The user/task prompt to hand to the coordinator.
            system_prompt: Optional system prompt override for this run.
            context_instructions: Extra instructions injected into the
                assembled context (e.g. retrieved memories).
            session_history: Prior turn history as a formatted string.
            metadata: Arbitrary key/value metadata attached to the run record.

        Yields:
            :class:`SupervisorEvent` — one per lifecycle state change plus
            wrapped :class:`AgentEvent` objects from the coordinator's loop.
        """
        coordinator = await self._get_or_spawn_coordinator()
        adaptor = _AgentLoopAdaptor(
            coordinator,
            max_turns=self._config.coordinator_max_iterations,
        )

        # Pull the tool_registry from the coordinator's broker so the
        # Supervisor can snapshot and freeze it.
        tool_registry = getattr(coordinator, "_broker", None)

        async for event in self._supervisor.run(
            session_id=session_id,
            prompt=prompt,
            tool_registry=tool_registry,
            system_prompt=system_prompt or self._config.coordinator_system_prompt,
            context_instructions=context_instructions,
            session_history=session_history,
            metadata=metadata,
            agent_loop=adaptor,
        ):
            yield event

        # If fresh_agent_per_run, tear down the coordinator so the next run
        # gets a clean slate.
        if self._config.fresh_agent_per_run:
            await self._stop_coordinator()

    async def recover(self) -> dict[str, int]:
        """Delegate crash recovery to the underlying Supervisor."""
        return await self._supervisor.recover()

    async def close(self) -> None:
        """Shut down coordinator and supervisor cleanly."""
        await self._stop_coordinator()
        self._supervisor.close()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get_or_spawn_coordinator(self) -> Agent:
        """Return the coordinator agent, spawning + starting it if needed."""
        async with self._lock:
            if self._coordinator is not None and not self._config.fresh_agent_per_run:
                return self._coordinator
            return await self._spawn_coordinator()

    async def _spawn_coordinator(self) -> Agent:
        """Spawn a fresh coordinator agent and call start()."""
        runtime = self._get_or_create_runtime()
        cfg = self._config

        agent = runtime.spawn(
            name=cfg.coordinator_name,
            model=cfg.coordinator_model,
            system_prompt=cfg.coordinator_system_prompt,
            memory_namespace=cfg.memory_namespace,
            can_delegate=cfg.can_delegate,
            delegate_allowlist=list(cfg.delegate_allowlist),
            max_iterations=cfg.coordinator_max_iterations,
            timeout_seconds=cfg.coordinator_timeout_seconds,
        )

        try:
            await agent.start()
        except Exception:
            logger.exception(
                "Failed to start coordinator agent '%s'",
                cfg.coordinator_name,
            )
            raise

        logger.info(
            "Coordinator agent '%s' (%s) started on model '%s'",
            cfg.coordinator_name,
            agent.id,
            cfg.coordinator_model,
        )
        self._coordinator = agent
        return agent

    async def _stop_coordinator(self) -> None:
        """Stop and discard the current coordinator agent."""
        if self._coordinator is None:
            return
        try:
            await self._coordinator.stop()
        except Exception:
            logger.debug("Error stopping coordinator agent", exc_info=True)
        finally:
            self._coordinator = None

    def _get_or_create_runtime(self) -> AgentRuntime:
        """Return (creating if needed) the underlying AgentRuntime."""
        if self._runtime is not None:
            return self._runtime

        self._runtime = AgentRuntime(user=self._user)
        return self._runtime

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def supervisor(self) -> Supervisor:
        """The underlying :class:`Supervisor` instance."""
        return self._supervisor

    @property
    def runtime(self) -> AgentRuntime | None:
        """The underlying :class:`AgentRuntime`, or ``None`` if not yet created."""
        return self._runtime

    @property
    def coordinator(self) -> Agent | None:
        """The current coordinator :class:`Agent`, or ``None`` if not running."""
        return self._coordinator
