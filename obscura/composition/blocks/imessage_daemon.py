"""obscura.composition.blocks.imessage_daemon — iMessage daemon (REPL only).

Starts the iMessage daemon (a separate ObscuraClient that polls
~/.obscura/agents.yaml for iMessage-triggered agents) when the REPL is
not running under a supervisor. When a supervisor is active, the
supervisor manages daemon agents and this block is a no-op.

Reads:
    session.client  — passed to start_imessage_daemon
    session.supervisor (skip if set)
    session.surface (REPL-only)

Writes:
    session.imessage_daemon_task

Resources:
    Registers daemon_task for cancellation on aclose.

Opt-out:
    1. session.surface != "repl" → return
    2. session.supervisor is not None → return (supervisor handles it)
    3. start_imessage_daemon raises or returns None → leave field None

Replaces these legacy callsites (DELETED in same change):
    - obscura/cli/_repl_loop.py inline start_imessage_daemon block
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from obscura.composition.session import AgentSession, SessionConfig

logger = logging.getLogger(__name__)


async def install_imessage_daemon(
    session: AgentSession,
    config: SessionConfig,  # noqa: ARG001
) -> None:
    """Start the iMessage daemon task when no supervisor is running."""
    if session.surface != "repl":
        return
    if session.supervisor is not None:
        # Supervisor manages daemon agents directly — don't double-start
        return

    try:
        from obscura.cli._daemon import start_imessage_daemon

        task = await start_imessage_daemon(session.client)
    except Exception:
        logger.debug("install_imessage_daemon: start failed", exc_info=True)
        return

    if task is None:
        return

    session.imessage_daemon_task = task
    session.register_resource(task, name="imessage_daemon_task")
    logger.info("install_imessage_daemon: started")
