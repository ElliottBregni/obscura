"""obscura.composition.blocks.session_registration — PID lock + signal handlers.

Registers the session with the cross-process session registry (PID lock
file under ~/.obscura/sessions/), installs SIGINT/SIGTERM handlers that
run queued shutdown callbacks, and registers an unregister hook for
LIFO teardown when the session aclose()s. Also runs a non-blocking
concurrent-session check so the user gets a heads-up when another REPL
is active in the same workspace.

Reads:
    session.session_id
    session.surface (REPL-only)

Writes:
    Side effects on the cross-process registry; no session fields.

Resources:
    Registers `unregister_session(session_id)` for LIFO teardown.

Opt-out:
    1. session.surface != "repl" → return immediately (signal handlers
       are process-global; only the REPL surface owns the process)

Replaces these legacy callsites (DELETED in same change):
    - obscura/cli/_repl_loop.py register_session/install_signal_handlers
      block (was inline before the input loop)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from obscura.composition.session import AgentSession, SessionConfig

logger = logging.getLogger(__name__)


async def install_session_registration(
    session: AgentSession,
    config: SessionConfig,  # noqa: ARG001
) -> None:
    """Register session lock + signal handlers; teardown unregisters."""
    if session.surface != "repl":
        return

    try:
        from obscura.core.session_utils import (
            check_concurrent_sessions,
            install_signal_handlers,
            register_session,
            register_shutdown_handler,
            unregister_session,
        )
    except Exception:
        logger.debug(
            "install_session_registration: imports failed",
            exc_info=True,
        )
        return

    sid = session.session_id

    # Mirror the cross-process session registration into the SQLite /
    # Postgres event store so session listings and turn persistence are
    # looking at the same logical session id.
    try:
        from obscura.core.db_factory import DatabaseFactory

        store = DatabaseFactory.create_event_store()
        existing = await store.get_session(sid)
        if existing is None:
            await store.create_session(
                sid,
                agent="repl",
                backend=session.config.backend,
                model=session.config.model or "",
                source="live",
                metadata={"surface": session.surface},
            )
            logger.info(
                "install_session_registration: event store session created sid=%s",
                sid[:12],
            )
        else:
            logger.info(
                "install_session_registration: event store session exists sid=%s status=%s",
                sid[:12],
                existing.status.value,
            )
        store.close()
    except Exception:
        logger.debug(
            "install_session_registration: event store session registration failed",
            exc_info=True,
        )

    try:
        register_session(
            sid,
            backend=session.config.backend,
            model=session.config.model or "",
        )
    except Exception:
        logger.debug(
            "install_session_registration: register_session failed",
            exc_info=True,
        )

    try:
        register_shutdown_handler(lambda: unregister_session(sid))
    except Exception:
        logger.debug(
            "install_session_registration: shutdown handler failed",
            exc_info=True,
        )

    try:
        install_signal_handlers()
    except Exception:
        logger.debug(
            "install_session_registration: signal handler install failed",
            exc_info=True,
        )

    # Non-blocking concurrent-session warning — informational only
    try:
        concurrent = check_concurrent_sessions(sid)
        if concurrent:
            logger.info(
                "Other Obscura sessions active: %s",
                ", ".join(str(c)[:12] for c in concurrent),
            )
    except Exception:
        logger.debug(
            "install_session_registration: concurrent check failed",
            exc_info=True,
        )

    # Register the unregister call for LIFO teardown
    async def _unregister() -> None:
        try:
            unregister_session(sid)
        except Exception:
            logger.debug(
                "install_session_registration: unregister_session failed",
                exc_info=True,
            )

    session.register_resource(_unregister, name="session_registration")
    logger.info("install_session_registration: registered sid=%s", sid[:12])
