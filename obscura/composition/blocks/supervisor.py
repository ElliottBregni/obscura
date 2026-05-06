"""obscura.composition.blocks.supervisor — multi-agent supervisor (REPL only).

Spawns the AgentSupervisor that runs declared agents from agents.yaml as
long-lived background tasks. The supervisor task is registered for LIFO
teardown so it cancels cleanly on session exit.

Reads:
    config.tools_enabled
    config.extras["supervise"]    — True if --supervise was passed
    config.extras["agent_infos"]  — list from `_discover_agent_infos()`

Writes:
    session.supervisor       — AgentSupervisor, or None
    session.supervisor_task  — asyncio.Task running supervisor.run_forever()

Resources:
    Registers supervisor_task for cancellation + await on session aclose.

Opt-out:
    1. session.surface != "repl" → return immediately
    2. supervise=False or no agent_infos → return immediately
    3. AgentSupervisor construction fails → log warning, leave fields None

Replaces these legacy callsites (DELETED in same change):
    - obscura/cli/_repl_loop.py supervisor spawn block
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from obscura.composition.session import AgentSession, SessionConfig

logger = logging.getLogger(__name__)


async def install_supervisor(
    session: AgentSession,
    config: SessionConfig,
) -> None:
    """Spawn AgentSupervisor as a background task on the session.

    See module docstring for full contract.
    """
    if session.surface != "repl":
        return
    if not config.tools_enabled:
        return

    if not config.extras.get("supervise"):
        return
    raw_agent_infos: Any = config.extras.get("agent_infos") or []
    if not raw_agent_infos:
        return
    agent_infos: list[Any] = list(raw_agent_infos)

    try:
        from obscura.agent.supervisor import AgentSupervisor
        from obscura.auth.cli_user import current_cli_user
        from obscura.core.paths import resolve_obscura_home

        agents_yaml = resolve_obscura_home() / "agents.yaml"
        sup_user = current_cli_user()
        supervisor = AgentSupervisor(
            config_path=agents_yaml,
            user=sup_user,
        )
        task = asyncio.create_task(
            supervisor.run_forever(),
            name="supervisor",
        )
        session.supervisor = supervisor
        session.supervisor_task = task
        session.register_resource(task, name="supervisor_task")

        logger.info(
            "install_supervisor: started with %d agent(s)",
            len(agent_infos),
        )
    except Exception:
        logger.exception("install_supervisor: spawn failed")
        session.supervisor = None
        session.supervisor_task = None
