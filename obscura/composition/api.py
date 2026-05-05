"""obscura.composition.api — `build_api_session` for the REST API.

Called per-request from `obscura.deps.ClientFactory.create_session()`.
Constructs an `AgentSession` with plugin tools registered (via the
shared `install_plugin_tools` block) so API requests have the same
tool surface as REPL — no more silent feature gap between surfaces.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from obscura.composition.blocks import install_plugin_tools
from obscura.composition.core import build_core_session
from obscura.composition.session import AgentSession, SessionConfig

if TYPE_CHECKING:
    from obscura.auth.models import AuthenticatedUser

logger = logging.getLogger(__name__)


async def build_api_session(
    config: SessionConfig,
    *,
    user: AuthenticatedUser,
    auth: Any = None,
) -> AgentSession:
    """Build a session for one API request.

    Pipeline:
      core: ObscuraClient + backend.start() (with MCP servers from config)
      extras:
        1. install_plugin_tools  (SAME block as REPL/A2A — no drift)

    Future blocks (vector memory, project hooks, capability filter)
    plug in here as they're extracted.
    """
    session = await build_core_session(
        config,
        surface="api",
        user=user,
        auth=auth,
    )
    await install_plugin_tools(session, config)
    return session
