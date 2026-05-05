"""obscura.composition.repl — `build_repl_session` for the interactive REPL.

The REPL is the heaviest surface (vector memory, browser bridge,
supervisor, KAIROS, iMessage daemon, prompt composition with memory
sections, etc.). The composition refactor extracts these block by
block.

Currently extracted into build_repl_session:
- install_plugin_tools     (capability-gated builtin plugins)
- install_system_tools     (system / memory / worktree / task / goal /
                            profile / arbiter / lsp / browser tool specs)
- install_browser_bridge   (Chrome side-panel attach, REPL-only)

NOT yet extracted for REPL (kept inline in cli/_repl_loop.py):
- vector memory init  — entangled with prompt composition (REPL uses
                        load_startup_memories during compose_system_prompt
                        BEFORE the session is built). Will move when
                        compose_system_prompt itself is extracted.
- project hooks       — same: kairos hook closure, channel hook closure
                        get computed before client construction today.
- repl callbacks (ask_user / permission_mode / plan_approval / user_interact)
- tool router, supervisor, KAIROS engine, iMessage daemon, UDS inbox,
  session registration

API and A2A boot modules are not subject to that constraint — they call
install_vector_memory + install_project_hooks today.

Migration tracker: see CLAUDE.md / composition refactor design.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from obscura.composition.blocks import (
    install_browser_bridge,
    install_kairos_engine,
    install_plugin_tools,
    install_repl_callbacks,
    install_supervisor,
    install_system_tools,
    install_tool_router,
)
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
    vector_store: Any = None,
    context_router: Any = None,
    turn_classifier: Any = None,
) -> AgentSession:
    """Build a session for the interactive REPL.

    The REPL passes any pre-initialized state (vector store, context
    router, project hooks) as kwargs — it has to init those upfront
    today because compose_system_prompt reads them. The composition
    blocks for vector_memory and project_hooks check `session.X is not
    None` and skip when already set, so there's no double-init.
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
    # Forward REPL-supplied state onto the session so blocks downstream
    # (and the REPL slash commands) can read it uniformly.
    if vector_store is not None:
        session.vector_store = vector_store
    if context_router is not None:
        session.context_router = context_router
    if turn_classifier is not None:
        session.turn_classifier = turn_classifier
    if project_hooks is not None:
        session.project_hooks = project_hooks

    await install_plugin_tools(session, config)
    await install_system_tools(session, config)
    await install_repl_callbacks(session, config)
    await install_browser_bridge(session, config)
    await install_supervisor(session, config)  # before kairos: kairos reads session.supervisor
    await install_kairos_engine(session, config)
    await install_tool_router(session, config)  # last: needs final tool registry
    return session
