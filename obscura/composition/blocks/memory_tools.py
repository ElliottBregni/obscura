"""obscura.composition.blocks.memory_tools — register memory tool specs.

Splits the `make_memory_tool_specs(user)` registration out of
`install_system_tools` so the dependency on `session.vector_store`
is expressed by block ORDERING rather than by hidden coupling inside
`install_system_tools`.

The previous design forced every surface to call
`install_vector_memory` before `install_system_tools` (because system
tools quietly checked `session.vector_store`). REPL had to invert the
order it would otherwise prefer just to satisfy that hidden contract.
Promoting memory tools to their own block lets every surface order:

    install_system_tools     # surface-independent, no dependencies
    install_vector_memory    # sets session.vector_store
    install_memory_tools     # depends on session.vector_store + user

Reads:
    config.tools_enabled
    session.vector_store (gates registration; None → skip)
    session.user (passed to `make_memory_tool_specs`; None → skip)

Writes:
    session.registry — adds memory tool specs via `session.add_tool()`
        (idempotent; re-running is a no-op)

Resources: none

Opt-out:
    1. config.tools_enabled is False → return immediately
    2. session.vector_store is None → return (vector memory opted out
       upstream, e.g. `OBSCURA_VECTOR_MEMORY=off`, no Qdrant configured,
       or the surface skipped `install_vector_memory` entirely — A2A
       does this today)
    3. session.user is None → return (unauth surface; memory tools need
       a user identity for namespacing)

Surface coverage: REPL + API. A2A skips this block (matches its skip of
`install_vector_memory`).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from obscura.composition.session import AgentSession, SessionConfig

logger = logging.getLogger(__name__)


async def install_memory_tools(
    session: AgentSession,
    config: SessionConfig,
) -> None:
    """Register memory tool specs onto the session.

    See module docstring for full contract.
    """
    if not config.tools_enabled:
        logger.debug("install_memory_tools: tools disabled, skipping")
        return

    if session.vector_store is None:
        logger.debug(
            "install_memory_tools: no vector_store on session, skipping",
        )
        return

    if session.user is None:
        logger.debug("install_memory_tools: no user on session, skipping")
        return

    from obscura.tools.memory_tools import make_memory_tool_specs

    registered = 0
    skipped = 0
    for spec in make_memory_tool_specs(session.user):
        if session.add_tool(spec):
            registered += 1
        else:
            skipped += 1

    logger.info(
        "install_memory_tools: registered=%d skipped=%d (surface=%s)",
        registered,
        skipped,
        session.surface,
    )
