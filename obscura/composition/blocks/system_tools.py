"""obscura.composition.blocks.system_tools — register @tool-decorated specs.

Registers the system tool surface that lives in `obscura/tools/`:
- `get_system_tool_specs()` — core system tools (shell, file, ui, etc.)
- `make_memory_tool_specs(user)` — memory tools, only if `session.vector_store` is set
- Lazy modules: worktree, task, goal, profile, arbiter, lsp, browser

Reads:
    config.tools_enabled
    session.vector_store (optional — gates memory tool registration)
    session.client._user (optional — passed to make_memory_tool_specs)

Writes:
    session.registry — adds tool specs via session.add_tool() (idempotent)

Resources: none

Opt-out:
    1. config.tools_enabled is False → return immediately
    2. Any individual module's import or getter failure → log+skip that
       module (matches REPL's pre-existing tolerance for missing optional
       deps like LSP servers or Chrome extension)

Replaces these legacy callsites (DELETED in same change):
    - obscura/cli/_repl_loop.py:299-324 (the system_tools building block)
    - obscura/cli/session.py:1218-1279 (_assemble_tools method body)

Surface coverage: all surfaces (REPL, API, A2A, MCP server). API and A2A
previously had ZERO @tool-decorated tools registered — adding system tools
on those surfaces is an intentional parity gain.
"""

from __future__ import annotations

import importlib
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from obscura.composition.session import AgentSession, SessionConfig

logger = logging.getLogger(__name__)


# Lazy-imported tool getters: (getter_name, module_path).
# These are imported per-call so a missing optional dep (Chrome extension
# for browser tools, LSP servers for lsp tools, etc.) doesn't block the
# whole block — failures log+skip per-module.
_LAZY_TOOL_GETTERS: tuple[tuple[str, str], ...] = (
    ("get_worktree_tool_specs", "obscura.tools.worktree"),
    ("get_task_tool_specs", "obscura.tools.task_tools"),
    ("get_goal_tool_specs", "obscura.tools.goal_tools"),
    ("get_profile_tool_specs", "obscura.tools.profile_tools"),
    ("get_arbiter_tool_specs", "obscura.tools.arbiter_tools"),
    ("get_lsp_tool_specs", "obscura.tools.lsp"),
    ("get_browser_tool_specs", "obscura.tools.browser"),
)


async def install_system_tools(
    session: AgentSession,
    config: SessionConfig,
) -> None:
    """Register all @tool-decorated system tool specs onto the session.

    See module docstring for full contract.
    """
    if not config.tools_enabled:
        logger.debug("install_system_tools: tools disabled, skipping")
        return

    registered = 0
    skipped = 0

    # Core system tools
    try:
        from obscura.tools.system import get_system_tool_specs

        for spec in get_system_tool_specs():
            if session.add_tool(spec):
                registered += 1
            else:
                skipped += 1
    except Exception:
        logger.debug(
            "install_system_tools: get_system_tool_specs failed", exc_info=True
        )

    # Memory tools — only if vector_store is configured AND user is set
    if session.vector_store is not None:
        user = getattr(session.client, "_user", None)
        if user is not None:
            try:
                from obscura.tools.memory_tools import make_memory_tool_specs

                for spec in make_memory_tool_specs(user):
                    if session.add_tool(spec):
                        registered += 1
                    else:
                        skipped += 1
            except Exception:
                logger.debug(
                    "install_system_tools: make_memory_tool_specs failed",
                    exc_info=True,
                )

    # Optional lazy-imported tool modules
    for getter_name, module_path in _LAZY_TOOL_GETTERS:
        try:
            mod = importlib.import_module(module_path)
            for spec in getattr(mod, getter_name)():
                if session.add_tool(spec):
                    registered += 1
                else:
                    skipped += 1
        except Exception:
            logger.debug(
                "install_system_tools: %s from %s failed",
                getter_name,
                module_path,
                exc_info=True,
            )

    logger.info(
        "install_system_tools: registered=%d skipped=%d (surface=%s)",
        registered,
        skipped,
        session.surface,
    )
