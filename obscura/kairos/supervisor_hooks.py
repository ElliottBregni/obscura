"""obscura.kairos.supervisor_hooks — Wire KairosEngine into the Supervisor lifecycle.

Registers two hooks on a SessionHookManager:
  - PRE_BUILD_CONTEXT/before  → engine.start()
  - POST_FINALIZE/after       → engine.stop()

Usage::

    from obscura.kairos.supervisor_hooks import register_kairos_hooks
    register_kairos_hooks(supervisor.hooks, kairos_engine)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from obscura.core.supervisor.types import SupervisorHookPoint

if TYPE_CHECKING:
    from obscura.core.supervisor.session_hooks import SessionHookManager
    from obscura.kairos.engine import KairosEngine

logger = logging.getLogger(__name__)


def register_kairos_hooks(
    hooks: SessionHookManager,
    engine: KairosEngine,
) -> None:
    """Wire KairosEngine start/stop into Supervisor lifecycle hooks.

    Hooks are registered as non-persistent (persist=False) since the engine
    is re-created each session and handlers are closures over the instance.

    Args:
        hooks: The SessionHookManager from the active Supervisor instance.
        engine: The KairosEngine instance to start/stop.

    """

    async def on_pre_build_context(ctx: dict[str, Any]) -> dict[str, Any]:
        """Start Kairos engine when supervisor begins building context."""
        if not engine.is_running:
            try:
                await engine.start()
            except Exception:
                logger.warning("Kairos engine failed to start", exc_info=True)
        return ctx

    async def on_post_finalize(ctx: dict[str, Any]) -> None:
        """Stop Kairos engine after supervisor finalizes the run."""
        if engine.is_running:
            try:
                await engine.stop()
            except Exception:
                logger.warning("Kairos engine failed to stop", exc_info=True)

    hooks.register(
        SupervisorHookPoint.PRE_BUILD_CONTEXT,
        "before",
        "kairos:start",
        on_pre_build_context,
        persist=False,
    )
    hooks.register(
        SupervisorHookPoint.POST_FINALIZE,
        "after",
        "kairos:stop",
        on_post_finalize,
        persist=False,
    )
    logger.debug("Kairos lifecycle hooks registered on supervisor")
