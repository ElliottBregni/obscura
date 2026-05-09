"""obscura.cli.tui.runtime — Public ``run_tui`` entry point.

The Click subcommand ``obscura tui`` calls :func:`run_tui` with a
:class:`TUIEngineConfig`. This module:

1. Loads ``.env`` files via :func:`obscura.cli._env_loader.bootstrap_env`.
2. Bootstraps the TUI engine handle via :func:`bootstrap_tui_session`.
3. Runs :class:`ObscuraTUIApp` inside the AgentSession async context
   manager so the session tears down cleanly on exit.

No lazy imports — every dependency is at module top.
"""

from __future__ import annotations

import logging

from obscura.cli._env_loader import bootstrap_env
from obscura.cli.tui.app import ObscuraTUIApp
from obscura.cli.tui.engine_adapter import (
    TUIEngineConfig,
    TUIEngineHandle,
    bootstrap_tui_session,
)

logger = logging.getLogger(__name__)

__all__ = ["run_tui"]


async def run_tui(cfg: TUIEngineConfig) -> int:
    """Bootstrap the engine, run the prompt-toolkit Application, exit cleanly.

    Returns the exit code (``0`` on clean exit, non-zero on fatal error).
    The caller is expected to be inside ``asyncio.run`` already — this is
    the canonical async entry point for the ``obscura tui`` subcommand.
    """
    # 1. Materialise .env into os.environ so subsequent backend builds
    # see API keys, MCP server settings, OBSCURA_* knobs.
    bootstrap_env()

    # 2. Build the TUI-shaped session. The four overlay callbacks are
    # left None here and attached by ``ObscuraTUIApp`` once the overlays
    # exist — the host_callbacks dict gets re-stamped on the session
    # before the first agent turn.
    handle: TUIEngineHandle = await bootstrap_tui_session(cfg)
    logger.info(
        "tui: starting sid=%s backend=%s",
        handle.session_id,
        cfg.backend,
    )

    # 3. Drive the Application inside the session's lifecycle so all
    # registered resources (supervisor task, daemons, plugin loaders)
    # tear down LIFO when we exit.
    try:
        async with handle.session:
            app = ObscuraTUIApp(handle)
            exit_code = await app.run()
        logger.info(
            "tui: clean shutdown sid=%s exit=%d",
            handle.session_id,
            exit_code,
        )
        return exit_code
    except Exception:
        logger.exception("tui: fatal error during run_tui")
        return 2
