"""obscura.composition.blocks.skill_context — OBSCURA.md + skills injection.

Loads OBSCURA.md / instructions / skill catalog into the system prompt
so every backend sees the same skill pool. Pulls from
``ContextLoader.load_system_prompt()`` and prepends the result to
``session.system_prompt``, mutating the running backend via
``session.update_system_prompt()`` so the next stream call sees it.

Reads:
    config.inject_claude_context  — opt-in flag (False by default)
    config.extras["skill_filter"] — optional list[str] of skill names
    config.extras["lazy_load_skills"] — optional bool
    session.client._backend (for the Backend enum the loader needs)
    session.capability_resolver (for capability gating of skills)
    session.system_prompt        — prepends to it

Writes:
    session.system_prompt   — replaced with prepended-context version
    session.client._system_prompt
    backend._system_prompt  — via session.update_system_prompt

Resources: none

Opt-out:
    config.inject_claude_context is False  → return immediately

Replaces ObscuraClient's inject_claude_context branch (the legacy
ContextLoader call inside __init__). ObscuraClient still supports the
flag for non-composition callers (Agent.start, tests, direct SDK use)
but composition surfaces should call this block instead.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from obscura.composition.session import AgentSession, SessionConfig

logger = logging.getLogger(__name__)


async def install_skill_context(
    session: AgentSession,
    config: SessionConfig,
) -> None:
    """Prepend OBSCURA.md + instructions + skill catalog to the system prompt."""
    if not config.inject_claude_context:
        return

    try:
        from obscura.core.context import ContextLoader
        from obscura.core.enums.agent import Backend

        backend_enum = Backend(config.backend) if config.backend else None
    except Exception:
        logger.debug("install_skill_context: backend resolution failed", exc_info=True)
        return

    if backend_enum is None:
        return

    skill_filter = config.extras.get("skill_filter")
    lazy_load = bool(config.extras.get("lazy_load_skills", False))

    try:
        loader = ContextLoader(
            backend_enum,
            lazy_load_skills=lazy_load,
            skill_filter=skill_filter,
            capability_resolver=session.capability_resolver,
            agent_id=session.session_id,
        )
        ctx = loader.load_system_prompt()
    except Exception:
        logger.debug("install_skill_context: ContextLoader failed", exc_info=True)
        return

    if not ctx:
        return

    base = session.system_prompt or config.system_prompt
    new_prompt = f"{ctx}\n\n{base}" if base else ctx
    session.update_system_prompt(new_prompt)
    logger.info(
        "install_skill_context: prepended (added=%d chars, total=%d)",
        len(ctx),
        len(new_prompt),
    )
