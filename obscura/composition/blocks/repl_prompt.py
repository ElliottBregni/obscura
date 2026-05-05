"""obscura.composition.blocks.repl_prompt — REPL system-prompt enrichment.

Composes the REPL-flavoured system prompt by reading state that other
blocks (vector_memory, project_hooks) have populated on the session,
plus REPL-specific bits (preferences.md, KAIROS prompt addition,
coordinator mode, wizard profiles, environment context).

Mutates session.system_prompt + session.client._system_prompt +
backend._system_prompt via session.update_system_prompt(). Both
Copilot and Claude backends read self._system_prompt per-stream, so
mutation propagates on the next turn.

Reads:
    config.system_prompt   — the base prompt to enrich
    config.extras["include_default_prompt"] (default True; env override
        OBSCURA_INCLUDE_DEFAULT_PROMPT=false)
    session.session_id
    session.vector_store   — for load_startup_memories (if set)
    session.context_router — for build_channels_prompt_section + system_channels
    session.client._user   — for memory tools and preferences scoping

Writes:
    session.system_prompt
    session.client._system_prompt
    backend._system_prompt (via session.update_system_prompt)

Resources: none

Opt-out:
    1. session.surface != "repl" → return immediately (REPL-specific
       sections like coordinator/wizard don't apply elsewhere)

Replaces these legacy callsites (DELETED in same change):
    - obscura/cli/_repl_loop.py compose_system_prompt block
      (load_obscura_memory + preferences.md + load_startup_memories +
      build_channels_prompt_section + compose_environment_context +
      KAIROS prompt addition + coordinator mode + wizard profiles)
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from obscura.composition.session import AgentSession, SessionConfig

logger = logging.getLogger(__name__)


async def install_repl_prompt_sections(
    session: AgentSession,
    config: SessionConfig,
) -> None:
    """Compose REPL system prompt and re-prime the running backend."""
    if session.surface != "repl":
        return

    # Lazy imports keep the block free of REPL-only deps when used elsewhere
    from obscura.agent import AGENT_TYPE_REGISTRY
    from obscura.agent.coordinator import (
        get_coordinator_system_prompt,
        is_coordinator_mode,
    )
    from obscura.cli.vector_memory_bridge import load_startup_memories
    from obscura.core.context import load_obscura_memory
    from obscura.core.paths import resolve_obscura_home
    from obscura.core.system_prompts import (
        compose_environment_context,
        compose_system_prompt,
    )
    from obscura.kairos.engine import KairosEngine, is_kairos_enabled
    from obscura.plugins.builtins import list_builtin_plugin_ids
    from obscura.tools.memory_tools import build_channels_prompt_section
    from obscura.tools.swarm import build_agent_catalog, load_agent_configs

    sid = session.session_id
    db_path = resolve_obscura_home() / "events.db"
    base = config.system_prompt
    include_default = not bool(config.extras.get("no_default_prompt", False))
    if os.environ.get("OBSCURA_INCLUDE_DEFAULT_PROMPT", "true").lower() == "false":
        include_default = False

    custom_sections: list[str] = []

    # Memory context (sqlite events.db)
    try:
        memory_context = load_obscura_memory(sid, db_path)
        if memory_context:
            custom_sections.append(memory_context)
    except Exception:
        logger.debug("install_repl_prompt_sections: load_obscura_memory failed", exc_info=True)

    # User identity & preferences
    try:
        prefs_path = resolve_obscura_home() / "memory" / "preferences.md"
        if prefs_path.exists():
            prefs_text = prefs_path.read_text().strip()
            if prefs_text:
                custom_sections.append(f"# User Identity & Preferences\n\n{prefs_text}")
    except Exception:
        logger.debug("install_repl_prompt_sections: preferences load failed", exc_info=True)

    # Vector memory startup recall (set by install_vector_memory)
    if session.vector_store is not None:
        try:
            vm_startup = load_startup_memories(session.vector_store, sid, top_k=3)
            if vm_startup:
                custom_sections.append(vm_startup)
        except Exception:
            logger.debug(
                "install_repl_prompt_sections: load_startup_memories failed",
                exc_info=True,
            )

    # Memory channel documentation (set by install_vector_memory)
    if session.context_router is not None:
        try:
            channels_doc = build_channels_prompt_section(session.context_router.channels)
            if channels_doc:
                custom_sections.append(channels_doc)
            sys_channel_ctx = session.context_router.get_system_channels()
            if sys_channel_ctx:
                custom_sections.append(sys_channel_ctx)
        except Exception:
            logger.debug(
                "install_repl_prompt_sections: channel docs failed",
                exc_info=True,
            )

    # Environment context (plugins, capabilities, agent types)
    try:
        env_section = compose_environment_context(
            plugin_ids=list_builtin_plugin_ids(),
            capabilities=[
                "shell.exec",
                "file.read",
                "file.write",
                "git.ops",
                "web.browse",
                "search.web",
                "security.scan",
            ],
            agent_types=list(AGENT_TYPE_REGISTRY.keys()),
        )
        if env_section:
            custom_sections.append(env_section)
    except Exception:
        logger.debug(
            "install_repl_prompt_sections: environment context failed",
            exc_info=True,
        )

    # KAIROS prompt addition
    try:
        if is_kairos_enabled():
            _probe = KairosEngine()
            kairos_sys = _probe.get_system_prompt_addition()
            if kairos_sys:
                custom_sections.append(kairos_sys)
    except Exception:
        logger.debug(
            "install_repl_prompt_sections: KAIROS prompt failed",
            exc_info=True,
        )

    # Coordinator mode
    try:
        if is_coordinator_mode():
            custom_sections.append(get_coordinator_system_prompt())
            try:
                catalog = build_agent_catalog(load_agent_configs())
                if catalog:
                    custom_sections.append(
                        f"## Available Specialist Agents\n\n{catalog}",
                    )
            except Exception:
                logger.debug(
                    "install_repl_prompt_sections: agent catalog failed",
                    exc_info=True,
                )
    except Exception:
        logger.debug(
            "install_repl_prompt_sections: coordinator section failed",
            exc_info=True,
        )

    combined = compose_system_prompt(
        base=base,
        include_default=include_default,
        custom_sections=custom_sections or None,
    )
    session.update_system_prompt(combined)

    logger.info(
        "install_repl_prompt_sections: composed (sections=%d, len=%d)",
        len(custom_sections),
        len(combined),
    )
