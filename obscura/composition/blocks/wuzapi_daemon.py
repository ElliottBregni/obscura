"""obscura.composition.blocks.wuzapi_daemon — wuzapi inbound bridge (REPL only).

Auto-starts the wuzapi-→-REPL inbound bridge during REPL boot when:

* ``session.surface == "repl"``
* No supervisor is running (supervisor manages daemon agents directly)
* ``[messaging.whatsapp].enabled = true`` and ``transport = "wuzapi"`` in
  obscura's ``config.toml``
* The wuzapi sidecar (``dev.obscura.wuzapi`` LaunchAgent) is currently running

When all four hold, this block:

1. Re-points wuzapi's webhook URL at this REPL's loopback receiver
   (idempotent; defends against a third party having stomped the config)
2. Enters the ``wuzapi_service`` async context manager (which binds a
   Starlette receiver on the configured port and forwards parsed events
   into ``channel_inject._queue``)
3. Registers the entered CM with ``session.register_resource`` so it gets
   torn down on REPL exit via LIFO

Reads:
    session.surface, session.supervisor
    ~/.obscura/config.toml :: [messaging.whatsapp] {enabled, transport, webhook_port}

Writes:
    (registers a resource on the session for LIFO teardown)

Opt-out:
    1. session.surface != "repl" → return
    2. session.supervisor is not None → return
    3. [messaging.whatsapp].enabled != true → return
    4. [messaging.whatsapp].transport != "wuzapi" → return
    5. wuzapi sidecar not running → log+return (graceful)
    6. user token missing → log+return (need `obscura whatsapp link`)
    7. uvicorn fails to bind the webhook port → log+return
"""

from __future__ import annotations

import logging
import tomllib
from typing import TYPE_CHECKING, Any, cast

from obscura.core.paths import resolve_obscura_home

if TYPE_CHECKING:
    from obscura.composition.session import AgentSession, SessionConfig

logger = logging.getLogger(__name__)

_DEFAULT_WEBHOOK_PORT = 18794


def _read_whatsapp_section() -> dict[str, Any]:
    """Best-effort load of ``[messaging.whatsapp]``. Empty dict on any error."""
    cfg_path = resolve_obscura_home() / "config.toml"
    if not cfg_path.is_file():
        return {}
    try:
        with cfg_path.open("rb") as fh:
            raw: dict[str, Any] = tomllib.load(fh)
    except Exception:
        return {}
    messaging_raw = raw.get("messaging", {})
    if not isinstance(messaging_raw, dict):
        return {}
    messaging: dict[str, Any] = cast("dict[str, Any]", messaging_raw)
    section = messaging.get("whatsapp", {})
    if not isinstance(section, dict):
        return {}
    return cast("dict[str, Any]", section)


async def install_wuzapi_daemon(
    session: AgentSession,
    config: SessionConfig,  # noqa: ARG001
) -> None:
    """Start the wuzapi inbound bridge if opted in via config."""
    if session.surface != "repl":
        return
    if session.supervisor is not None:
        return

    section = _read_whatsapp_section()
    if not bool(section.get("enabled", False)):
        return
    if str(section.get("transport", "")).strip().lower() != "wuzapi":
        return

    webhook_port = int(section.get("webhook_port", _DEFAULT_WEBHOOK_PORT))

    # Probe the sidecar — don't crash REPL boot if wuzapi is down.
    try:
        from obscura.integrations.whatsapp.wuzapi import lifecycle
        if not lifecycle.status().is_running:
            logger.info(
                "install_wuzapi_daemon: sidecar not running, skipping bridge "
                "(run `obscura whatsapp install` then `obscura whatsapp link`)"
            )
            return
    except Exception:
        logger.debug("install_wuzapi_daemon: lifecycle probe failed", exc_info=True)
        return

    # Auto-configure webhook on every REPL boot. Idempotent + cheap.
    # If this fails (link expired, network blip), proceed anyway — the
    # webhook URL is durable in wuzapi's DB across restarts, so the last
    # successful set still applies.
    from obscura.integrations.whatsapp.wuzapi.client import (
        WuzapiClient,
        WuzapiError,
    )
    from obscura.integrations.whatsapp.wuzapi.setup import load_user_token

    try:
        async with WuzapiClient(token=load_user_token()) as c:
            await c.set_webhook(
                f"http://127.0.0.1:{webhook_port}/inbound",
                events=["Message"],
            )
    except WuzapiError:
        logger.warning(
            "install_wuzapi_daemon: webhook auto-configure failed; "
            "using whatever wuzapi has stored"
        )
    except Exception:
        logger.debug(
            "install_wuzapi_daemon: webhook auto-configure failed", exc_info=True
        )

    # Enter the service CM; register for LIFO teardown on session aclose.
    try:
        from obscura.integrations.whatsapp.wuzapi.service import wuzapi_service

        cm = wuzapi_service(webhook_port=webhook_port)
        await cm.__aenter__()
    except Exception:
        logger.warning(
            "install_wuzapi_daemon: failed to start webhook receiver "
            "(port %d may be in use)", webhook_port, exc_info=True,
        )
        return

    session.register_resource(cm, name="wuzapi_service")
    logger.info(
        "install_wuzapi_daemon: bridge live on 127.0.0.1:%d/inbound", webhook_port
    )
