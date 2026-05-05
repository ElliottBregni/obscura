"""obscura.composition.blocks.project_hooks — load project-level hooks.

Builds a HookRegistry from the user's .obscura/hooks/ directory and adds
two synthesised hooks:
- A memory-channel TOOL_CALL hook (if session.context_router is set)
- KAIROS tool/turn hooks (if KAIROS is enabled — engine wiring is deferred
  until the kairos block exists; the closures already check for None)

Reads:
    session.context_router   — for memory-channel hook
    session.tool_router      — for memory-channel hook (best effort)
    workspace .obscura/hooks/

Writes:
    session.project_hooks   — HookRegistry, or None when nothing loaded

Resources: none

Opt-out: when load_all_hooks returns 0 entries AND no context_router AND
KAIROS disabled → leaves project_hooks=None.

Critical: this block runs AFTER build_core_session. The agent loop reads
hooks at run_loop time (via make_agent_loop kwargs from client._hooks),
not at backend construction time, so it is safe to set client._hooks here
post-build.

Replaces these legacy callsites (DELETED in same change):
    - obscura/cli/_repl_loop.py:495-540 (hook loading + channel hook + kairos hooks)
    - obscura/cli/session.py:1443-1502 (duplicate alt-path)

Surface coverage: REPL + API. A2A is short-lived and doesn't use project
hooks today.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from obscura.composition.session import AgentSession, SessionConfig

logger = logging.getLogger(__name__)


async def install_project_hooks(
    session: AgentSession,
    config: SessionConfig,  # noqa: ARG001
) -> None:
    """Load project hooks and synthesise channel/KAIROS hooks.

    See module docstring for full contract.
    """
    from obscura.core.enums.agent import AgentEventKind
    from obscura.core.hooks import HookRegistry
    from obscura.core.settings import load_all_hooks
    from obscura.kairos.engine import is_kairos_enabled

    project_hooks: Any = None

    # 1. Load disk-defined hooks
    try:
        registry = load_all_hooks()
        if registry.count > 0:
            project_hooks = registry
    except Exception:
        logger.debug("install_project_hooks: load_all_hooks failed", exc_info=True)

    # 2. Synthesise the memory-channel TOOL_CALL hook
    context_router = session.context_router
    if context_router is not None:
        if project_hooks is None:
            project_hooks = HookRegistry()

        def _channel_tool_signal(event: Any) -> None:
            try:
                context_router.update_signals_from_event(event)
                tool_router = session.tool_router
                if tool_router is not None and context_router.signals.file_paths:
                    tool_router.set_file_context(list(context_router.signals.file_paths))
            except Exception:
                logger.debug("channel_tool_signal hook failed", exc_info=True)

        project_hooks.add_after(_channel_tool_signal, AgentEventKind.TOOL_CALL)

    # 3. Synthesise KAIROS hooks (engine wired by the kairos block later;
    # closures defensively read from session.kairos_engine which is None
    # until then — harmless inert no-ops)
    try:
        if is_kairos_enabled():
            if project_hooks is None:
                project_hooks = HookRegistry()

            def _kairos_tool_hook(event: Any) -> None:
                engine = getattr(session, "kairos_engine", None)
                if engine is not None and getattr(engine, "is_running", False):
                    tool = getattr(event, "tool_name", "") or ""
                    args = str(getattr(event, "tool_input", "") or "")[:80]
                    try:
                        engine.log_tool_use(tool, args)
                    except Exception:
                        logger.debug("kairos_tool_hook failed", exc_info=True)

            def _kairos_turn_hook(_event: Any) -> None:
                engine = getattr(session, "kairos_engine", None)
                if engine is not None and getattr(engine, "is_running", False):
                    try:
                        engine.log_agent_event("turn_complete")
                    except Exception:
                        logger.debug("kairos_turn_hook failed", exc_info=True)

            project_hooks.add_after(_kairos_tool_hook, AgentEventKind.TOOL_CALL)
            project_hooks.add_after(_kairos_turn_hook, AgentEventKind.TURN_COMPLETE)
    except Exception:
        logger.debug("install_project_hooks: KAIROS wiring failed", exc_info=True)

    session.project_hooks = project_hooks

    # Rebind on the underlying client so make_agent_loop sees them at
    # stream_loop time. Hooks are read fresh per-loop, not baked into
    # backend construction, so post-build rebinding is correct.
    if project_hooks is not None:
        session.client._hooks = project_hooks  # pyright: ignore[reportPrivateUsage]

    logger.info(
        "install_project_hooks: hooks=%s channel_router=%s (surface=%s)",
        "set" if project_hooks else "none",
        "yes" if context_router else "no",
        session.surface,
    )
