"""obscura.composition.blocks.kairos — KAIROS background daemon (REPL only).

Initializes the KAIROS proactive/dream-cycle engine when enabled. If a
supervisor is present, the engine is registered with the supervisor's
hook system instead of being started directly (the supervisor manages
its lifecycle). Otherwise the engine is started immediately.

Reads:
    is_kairos_enabled() — env + .obscura/settings.json
    session.supervisor   — if set, register hooks instead of start()

Writes:
    session.kairos_engine

Resources:
    Registers kairos_engine.stop for teardown when started directly.

Opt-out:
    1. session.surface != "repl" → return immediately
    2. is_kairos_enabled() returns False → return
    3. KairosEngine() construction fails → log debug, return

Order constraint: install_supervisor must run BEFORE this block so
session.supervisor is set when this block reads it.

Loop registration is intentionally deferred — `kairos_engine
.register_agent_loop(loop)` requires a live AgentLoop instance, which
only exists during stream_loop. The caller is responsible for that
registration. Track in session.kairos_engine.agent_loop_pending or
similar if needed.

Replaces these legacy callsites (DELETED in same change):
    - obscura/cli/_repl_loop.py KAIROS engine init + register_kairos_hooks
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from obscura.composition.session import AgentSession, SessionConfig

logger = logging.getLogger(__name__)


async def install_kairos_engine(
    session: AgentSession,
    config: SessionConfig,  # noqa: ARG001
) -> None:
    """Initialize KAIROS engine + wire to supervisor or start directly.

    See module docstring for full contract.
    """
    if session.surface != "repl":
        return

    try:
        from obscura.kairos.engine import KairosEngine, is_kairos_enabled

        if not is_kairos_enabled():
            return

        engine = KairosEngine()
    except Exception:
        logger.debug("install_kairos_engine: init failed", exc_info=True)
        return

    session.kairos_engine = engine

    # If a supervisor is running, hand the engine to its hook system.
    # Otherwise, start it directly and register stop() for teardown.
    if session.supervisor is not None and hasattr(session.supervisor, "hooks"):
        try:
            from obscura.kairos.supervisor_hooks import register_kairos_hooks

            register_kairos_hooks(session.supervisor.hooks, engine)
            logger.info("install_kairos_engine: hooks registered with supervisor")
        except Exception:
            logger.debug(
                "install_kairos_engine: supervisor hook registration failed",
                exc_info=True,
            )
        return

    try:
        await engine.start()
    except Exception:
        logger.debug("install_kairos_engine: engine.start failed", exc_info=True)
        return

    # Register teardown — the engine has stop() (sync or async)
    if hasattr(engine, "stop"):
        async def _stop_engine() -> None:
            res = engine.stop()
            if hasattr(res, "__await__"):
                await res

        session.register_resource(_stop_engine, name="kairos_engine_stop")

    logger.info("install_kairos_engine: started")
