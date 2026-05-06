"""obscura.composition.blocks.vector_memory — vector store + channel router init.

Sets up the user's semantic memory (Qdrant or SQLite-backed VectorMemoryStore)
and the memory-channel router that routes turns into named channels.

Reads:
    config.tools_enabled
    env OBSCURA_VECTOR_MEMORY=off (explicit opt-out)
    session.client._user (user identity for store namespacing)

Writes:
    session.vector_store      — VectorMemoryStore instance, or None
    session.context_router    — ContextRouter, or None when no channels
    session.turn_classifier   — TurnClassifier, or None when no channels

Resources:
    Registers vector_store for teardown if it has aclose()/close().

Opt-out:
    1. config.tools_enabled is False → return immediately
    2. env OBSCURA_VECTOR_MEMORY=off → return immediately
    3. No authenticated user on session.client → log+return
    4. init_vector_store() returns None (no Qdrant/SQLite configured) → return

Replaces these legacy callsites (DELETED in same change):
    - obscura/cli/_repl_loop.py:202-218 (init + decay + channel router)
    - obscura/cli/session.py:1080-1099 (duplicate alt-path)

Surface coverage: REPL + API. A2A is short-lived per-task and skips
vector memory by default. Surface block can be opted-in for A2A later.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from obscura.composition.session import AgentSession, SessionConfig

logger = logging.getLogger(__name__)


async def install_vector_memory(
    session: AgentSession,
    config: SessionConfig,
) -> None:
    """Initialize vector store + channel router on the session.

    See module docstring for full contract.
    """
    if not config.tools_enabled:
        logger.debug("install_vector_memory: tools disabled, skipping")
        return

    # Defence-in-depth: if a surface (e.g. REPL) pre-set vector_store before
    # build_*_session, don't double-init.
    if session.vector_store is not None:
        logger.debug(
            "install_vector_memory: session.vector_store already set, skipping",
        )
        return

    if os.environ.get("OBSCURA_VECTOR_MEMORY", "").lower() == "off":
        logger.debug("install_vector_memory: OBSCURA_VECTOR_MEMORY=off, skipping")
        return

    user = getattr(session.client, "_user", None)
    if user is None:
        logger.debug("install_vector_memory: no user on session.client, skipping")
        return

    # Step 1: Initialize the vector store. Returns None if no Qdrant/SQLite
    # is configured — that's a soft opt-out, not an error.
    try:
        from obscura.cli.vector_memory_bridge import (
            init_vector_store,
            run_startup_maintenance,
        )

        vector_store = init_vector_store(user)
    except Exception:
        logger.exception("install_vector_memory: init_vector_store failed")
        return

    if vector_store is None:
        logger.debug("install_vector_memory: no vector store configured")
        return

    session.vector_store = vector_store

    # Register teardown (if the store exposes a close handle)
    if hasattr(vector_store, "aclose") or hasattr(vector_store, "close"):
        session.register_resource(vector_store, name="vector_store")

    # Step 2: Background decay maintenance — best effort
    try:
        run_startup_maintenance(vector_store)
    except Exception:
        logger.debug("install_vector_memory: startup maintenance failed", exc_info=True)

    # Step 3: Memory channel router (only when channels.yaml has entries)
    try:
        from obscura.memory_channels import (
            ContextRouter,
            TurnClassifier,
            load_channels_from_config,
        )

        channels = load_channels_from_config()
        if channels:
            session.context_router = ContextRouter(channels, vector_store)
            session.turn_classifier = TurnClassifier(channels)
    except Exception:
        logger.debug(
            "install_vector_memory: channel router init failed",
            exc_info=True,
        )

    logger.info(
        "install_vector_memory: vector_store=%s context_router=%s (surface=%s)",
        type(session.vector_store).__name__,
        "yes" if session.context_router else "no",
        session.surface,
    )
