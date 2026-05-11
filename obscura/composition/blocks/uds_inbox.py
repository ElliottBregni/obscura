"""obscura.composition.blocks.uds_inbox — cross-session message inbox (REPL only).

Starts the Unix-domain-socket inbox so other Obscura sessions can post
messages to this session (used by the supervisor + cross-session mention
features). The inbox is registered for teardown so its socket is closed
on session aclose.

Reads:
    config.tools_enabled
    session.session_id
    session.surface (REPL-only)

Writes:
    session.uds_inbox  — UDSInbox instance, or None

Resources:
    Registers UDSInbox stop/aclose for teardown.

Opt-out:
    1. session.surface != "repl" → return immediately
    2. config.tools_enabled is False → return
    3. UDSInbox construction or start fails → log debug, leave field None

Replaces these legacy callsites (DELETED in same change):
    - obscura/cli/_repl_loop.py UDSInbox init block
"""

from __future__ import annotations

import contextlib
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from obscura.composition.session import AgentSession, SessionConfig

logger = logging.getLogger(__name__)


async def install_uds_inbox(
    session: AgentSession,
    config: SessionConfig,
) -> None:
    """Start the cross-session UDS inbox on the REPL session."""
    if session.surface != "repl":
        return
    if not config.tools_enabled:
        return

    try:
        from obscura.kairos.uds_messaging import UDSInbox

        inbox = UDSInbox(session.session_id)
    except Exception:
        logger.debug("install_uds_inbox: construction failed", exc_info=True)
        return

    def _on_peer_message(payload: dict[str, Any]) -> None:
        try:
            text = payload.get("text") or payload.get("message") or ""
            if not text:
                return

            # Resolve platform: use explicit "platform" key when set by the
            # gateway's channel fanout (e.g. "telegram", "whatsapp"), fall
            # back to "peer" for generic cross-session messages.
            platform = payload.get("platform") or "peer"

            # Sender label: prefer human-readable display_name, then "from",
            # then "from_session" (which is often the platform name itself).
            sender_id = (
                payload.get("sender_id")
                or payload.get("from_session")
                or payload.get("from")
                or "peer"
            )
            display_name = (
                payload.get("display_name")
                or payload.get("from")
                or sender_id
            )

            logger.info("[%s:%s] %s", platform, display_name[:24], text[:120])

            # Inject into the REPL channel so it races with keyboard input.
            from obscura.integrations.messaging.channel_inject import (
                ChannelMessage,
                push_channel_message,
            )

            async def _noop_reply(response: str) -> bool:  # noqa: ARG001
                return True

            pushed = push_channel_message(ChannelMessage(
                platform=platform,
                sender_id=sender_id,
                display_name=display_name,
                text=text,
                reply_fn=_noop_reply,
            ))
            if not pushed:
                logger.warning("install_uds_inbox: channel queue full, peer message dropped")
        except Exception:
            logger.debug("install_uds_inbox: on_peer_message failed", exc_info=True)

    try:
        await inbox.start(on_message=_on_peer_message)
    except Exception:
        logger.debug("install_uds_inbox: start failed", exc_info=True)
        return

    session.uds_inbox = inbox

    async def _stop_inbox() -> None:
        with contextlib.suppress(Exception):
            res = inbox.stop()
            if hasattr(res, "__await__"):
                await res

    session.register_resource(_stop_inbox, name="uds_inbox")
    logger.info("install_uds_inbox: started")
