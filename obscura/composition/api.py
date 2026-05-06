"""obscura.composition.api — `build_api_session` for the REST API.

Called per-request from `obscura.deps.ClientFactory.create_session()`.
Constructs an `AgentSession` with the same plugin + system tool surface
as REPL, plus optional vector memory and project hooks. This brings the
API to feature parity with REPL (previously API had ZERO plugin/system
tools registered — agents could only call MCP tools).

Pipeline:
    core: ObscuraClient + backend.start() (with MCP servers from config)
    extras:
        1. install_plugin_tools    (SAME block as REPL/A2A)
        2. install_system_tools    (SAME block as REPL/A2A)
        3. install_vector_memory   (sets session.vector_store; skipped if
                                    no Qdrant configured for user)
        4. install_memory_tools    (memory tool specs; depends on
                                    session.vector_store)
        5. install_project_hooks   (server-side audit/telemetry hooks)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from obscura.composition.blocks import (
    install_memory_tools,
    install_project_hooks,
    install_skill_context,
    install_tool_router,
    install_vector_memory,
)
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
    """Build a session for one API request."""
    session = await build_core_session(
        config,
        surface="api",
        user=user,
        auth=auth,
    )
    # MCP servers + plugin + system tools: build_core_session installs
    # all three before backend.start (Copilot/Claude SDK requirement).
    await install_vector_memory(session, config)
    await install_memory_tools(session, config)
    await install_project_hooks(session, config)
    await install_skill_context(session, config)
    await install_tool_router(session, config)
    return session
