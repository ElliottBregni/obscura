"""obscura.composition.blocks.tool_router — eval-driven tool router.

Constructs a `ToolRouter` from the capability index built by the plugin
block (`session.capability_resolver`) and binds it to the backend if the
backend implements `ToolRouterCapable`. The router gates which tools the
LLM sees per call based on ranked relevance.

Reads:
    config.tools_enabled
    config.backend
    session.capability_resolver  (set by install_plugin_tools)
    session.client._backend       (must be ToolRouterCapable)

Writes:
    session.tool_router

Resources: none

Opt-out:
    1. config.tools_enabled is False → return immediately
    2. session.capability_resolver is None (plugin block opted out) → return
    3. backend is not ToolRouterCapable → return (graceful, no error)

Replaces these legacy callsites (DELETED in same change):
    - obscura/cli/_repl_loop.py:480-521 (inline tool router after async-with)
    - obscura/cli/session.py:1505-1536 (`_wire_tool_router` method body)

Surface coverage: all surfaces. The router improves tool selection on
every backend that supports it; no surface-specific behaviour.

Order constraint: must run AFTER install_plugin_tools so
session.capability_resolver is set with the cap_index.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from obscura.composition.session import AgentSession, SessionConfig

logger = logging.getLogger(__name__)


async def install_tool_router(
    session: AgentSession,
    config: SessionConfig,
) -> None:
    """Install the eval-driven tool router on the backend.

    See module docstring for full contract.
    """
    if not config.tools_enabled:
        logger.debug("install_tool_router: tools disabled, skipping")
        return

    if session.capability_resolver is None:
        logger.debug(
            "install_tool_router: no capability_resolver, skipping",
        )
        return

    try:
        from obscura.core.compiler.compiled import ToolRoutingConfig
        from obscura.core.tool_router import ToolRouter
        from obscura.core.tool_score_index import ToolScoreIndex
        from obscura.core.types import ToolRouterCapable

        backend = session.client._backend  # pyright: ignore[reportPrivateUsage]
        if not isinstance(backend, ToolRouterCapable):
            logger.debug(
                "install_tool_router: backend %s is not ToolRouterCapable",
                type(backend).__name__,
            )
            return

        routing_config = ToolRoutingConfig()
        score_index = ToolScoreIndex()
        cap_index = session.capability_resolver.capability_index

        router = ToolRouter.from_capability_index(
            config=routing_config,
            score_index=score_index,
            capability_index=cap_index,
            backend=config.backend,
        )

        backend.set_tool_router(router)
        session.tool_router = router

        logger.info("install_tool_router: router bound (surface=%s)", session.surface)
    except Exception:
        logger.debug("install_tool_router: failed", exc_info=True)
