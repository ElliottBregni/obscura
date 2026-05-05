"""obscura.composition.blocks.browser_bridge — attach browser extension.

REPL-only. Detects whether the Obscura Chrome side panel is running, and
if so attaches via the native host's Unix socket and registers the ~27
`browser_*` tool specs onto the session. Stores the bridge client on
`session.browser_bridge` so the REPL can introspect status; registers the
client for LIFO teardown so the socket closes cleanly on session exit.

Reads:
    config.tools_enabled
    session.surface (REPL-only block)

Writes:
    session.browser_bridge   — BrowserBridgeClient, or None
    session.registry         — adds browser_* tool specs

Resources:
    Registers BrowserBridgeClient for async teardown (closes the Unix
    socket connection).

Opt-out:
    1. session.surface != "repl" → return immediately
    2. config.tools_enabled is False → return immediately
    3. Extension not running (attach_if_running returns (None, None)) →
       session.browser_bridge=None, no tools registered, no error

Replaces these legacy callsites (DELETED in same change):
    - obscura/cli/_repl_loop.py:639-651 (attach_if_running + status banner setup)

Surface coverage: REPL only. API and A2A are not local interactive
sessions and do not have a browser extension to talk to.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from obscura.composition.session import AgentSession, SessionConfig

logger = logging.getLogger(__name__)


async def install_browser_bridge(
    session: AgentSession,
    config: SessionConfig,
) -> None:
    """Attach to Chrome extension (if running) and register browser tools.

    See module docstring for full contract.
    """
    if session.surface != "repl":
        return
    if not config.tools_enabled:
        return

    def _register(spec: Any) -> None:
        # attach_if_running expects a None-returning callable; session.add_tool
        # returns a bool (newly-added vs duplicate) — wrap to drop the bool.
        session.add_tool(spec)

    try:
        from obscura.integrations.browser.client import attach_if_running

        bridge_client, status = await attach_if_running(_register)
    except Exception:
        logger.debug("install_browser_bridge: attach_if_running failed", exc_info=True)
        return

    if bridge_client is None:
        # Extension not running — soft opt-out
        return

    session.browser_bridge = bridge_client

    # Register for teardown so the Unix socket closes on session exit
    if hasattr(bridge_client, "aclose") or hasattr(bridge_client, "close") or hasattr(
        bridge_client, "__aexit__",
    ):
        session.register_resource(bridge_client, name="browser_bridge")

    logger.info(
        "install_browser_bridge: attached (status=%s) tools=%d",
        bool(status),
        len([t for t in session.registry.all() if t.name.startswith("browser_")]),
    )
