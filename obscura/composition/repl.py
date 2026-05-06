"""obscura.composition.repl — `build_repl_session` for the interactive REPL.

The REPL is the heaviest surface (vector memory, browser bridge,
supervisor, KAIROS, iMessage daemon, prompt composition with memory
sections, etc.). The composition refactor extracts these block by
block.

Currently extracted into build_repl_session:
- install_plugin_tools         (capability-gated builtin plugins)
- install_system_tools         (system / worktree / etc tool specs)
- install_vector_memory        (Qdrant store + channel router)
- install_memory_tools         (memory tool specs; depends on vector_store)
- install_project_hooks        (.obscura/hooks/ + memory channel + KAIROS)
- install_repl_prompt_sections (REPL system prompt enrichment with
                                 memory/channels/env/KAIROS sections;
                                 mutates backend system_prompt post-build)
- install_repl_callbacks       (ask_user / plan_approval / user_interact)
- install_browser_bridge       (Chrome side-panel attach)
- install_supervisor           (multi-agent supervisor)
- install_kairos_engine        (KAIROS proactive daemon)
- install_imessage_daemon      (iMessage integration)
- install_uds_inbox            (cross-session messaging)
- install_session_registration (PID lock + signal handlers)
- install_tool_router          (eval-driven tool router)

Still inline in cli/_repl_loop.py (small remainder):
- permission_mode_callback (REPLContext-coupled)
- prompt UI loop (terminal widgets, slash commands)
- daemon restart polling (stateful, doesn't fit one-shot block model)

Migration tracker: see CLAUDE.md / composition refactor design.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from obscura.composition.blocks import (
    install_browser_bridge,
    install_imessage_daemon,
    install_kairos_engine,
    install_memory_tools,
    install_plugin_tools,
    install_project_hooks,
    install_repl_callbacks,
    install_repl_prompt_sections,
    install_session_registration,
    install_skill_context,
    install_supervisor,
    install_system_tools,
    install_tool_router,
    install_uds_inbox,
    install_vector_memory,
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
) -> AgentSession:
    """Build a session for the interactive REPL.

    Pipeline (extras run in dependency order):
      core: ObscuraClient + backend.start() (with bare base prompt;
            install_repl_prompt_sections enriches it post-build)
      extras:
        1. install_plugin_tools     — capability resolver + plugin specs
        2. install_system_tools     — @tool-decorated specs (no memory)
        3. install_vector_memory    — vector_store + context_router
        4. install_memory_tools     — memory tool specs (reads
                                       session.vector_store; skipped when
                                       vector_memory opted out)
        5. install_project_hooks    — .obscura/hooks + channel hook
                                       (closes over session.context_router)
        6. install_repl_prompt_sections — composes the full REPL prompt
                                       (memory + channels + env + kairos +
                                       coordinator + wizard) and mutates
                                       backend._system_prompt
        7. install_repl_callbacks   — ask_user / plan_approval / user_interact
        8. install_browser_bridge   — Chrome extension (best-effort)
        9. install_supervisor       — multi-agent supervisor (--supervise)
       10. install_kairos_engine    — KAIROS daemon
       11. install_imessage_daemon  — iMessage daemon (skipped if supervisor)
       12. install_uds_inbox        — cross-session UDS messaging
       13. install_session_registration — PID lock + signal handlers
       14. install_tool_router      — eval-driven tool router (last:
                                       sees full registry)
    """
    session = await build_core_session(
        config,
        surface="repl",
        user=user,
        host_callbacks=host_callbacks,
        session_id=session_id,
        preregistered_tools=preregistered_tools,
    )
    # MCP servers: build_core_session calls install_mcp_servers internally
    # so the block runs before backend.start (Claude SDK requirement).
    await install_plugin_tools(session, config)
    await install_system_tools(session, config)
    await install_vector_memory(session, config)
    await install_memory_tools(session, config)
    await install_project_hooks(session, config)
    await install_repl_prompt_sections(session, config)
    await install_skill_context(
        session, config
    )  # after prompt_sections: wraps composed prompt
    await install_repl_callbacks(session, config)
    await install_browser_bridge(session, config)
    await install_supervisor(session, config)
    await install_kairos_engine(session, config)
    await install_imessage_daemon(session, config)
    await install_uds_inbox(session, config)
    await install_session_registration(session, config)
    await install_tool_router(session, config)
    return session
