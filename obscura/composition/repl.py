"""obscura.composition.repl — `build_repl_session` for the interactive REPL.

The REPL is the heaviest surface (vector memory, browser bridge,
supervisor, KAIROS, iMessage daemon, prompt composition with memory
sections, etc.). The composition refactor extracts these block by
block; for the first pass only `install_plugin_tools` is moved here.

The remaining REPL boot logic stays inline in
`obscura/cli/_repl_loop.py` for now — each subsequent extracted block
collapses more of it into this module.

Migration tracker: see CLAUDE.md / composition refactor design.
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


async def build_repl_session(
    config: SessionConfig,
    *,
    user: AuthenticatedUser | None = None,
    host_callbacks: dict[str, Any] | None = None,
    session_id: str | None = None,
    preregistered_tools: list[Any] | None = None,
    project_hooks: Any = None,
) -> AgentSession:
    """Build a session for the interactive REPL.

    Pipeline (current — grows as more blocks are extracted):
      core: ObscuraClient + backend.start() (with MCP servers from config)
      extras:
        1. install_plugin_tools  (SAME block as API/A2A — no drift)

    Future blocks (vector memory, project hooks, browser bridge,
    supervisor, KAIROS, iMessage daemon) move here from
    `cli/_repl_loop.py` as they're extracted.
    """
    session = await build_core_session(
        config,
        surface="repl",
        user=user,
        host_callbacks=host_callbacks,
        session_id=session_id,
        preregistered_tools=preregistered_tools,
        hooks=project_hooks,
    )
    if project_hooks is not None:
        # Stash on the session so tool router and other extras can read it.
        # Hook block migration will absorb this.
        session.project_hooks = project_hooks
    await install_plugin_tools(session, config)
    return session
