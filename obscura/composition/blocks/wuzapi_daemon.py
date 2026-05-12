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
    """Start the wuzapi inbound bridge if opted in via config.

    Unlike the iMessage daemon block, this one does NOT gate on
    ``session.supervisor`` — the wuzapi bridge is an HTTP receiver that
    push_channel_message-feeds the REPL queue, not a supervised agent
    client. The supervisor pattern doesn't apply.

    Every meaningful early-return logs at INFO with an explicit ``[wuzapi]``
    prefix so the banner output makes it obvious which gate the block
    hit during startup.
    """
    print("[wuzapi] block entered", flush=True)
    if session.surface != "repl":
        print(f"[wuzapi] skip: surface={session.surface!r} (need 'repl')", flush=True)
        return

    section = _read_whatsapp_section()
    if not bool(section.get("enabled", False)):
        print("[wuzapi] skip: [messaging.whatsapp].enabled is not true", flush=True)
        return
    if str(section.get("transport", "")).strip().lower() != "wuzapi":
        print(
            f"[wuzapi] skip: transport={section.get('transport')!r} (need 'wuzapi')",
            flush=True,
        )
        return

    webhook_port = int(section.get("webhook_port", _DEFAULT_WEBHOOK_PORT))

    # Probe the sidecar — don't crash REPL boot if wuzapi is down.
    try:
        from obscura.integrations.whatsapp.wuzapi import lifecycle
        if not lifecycle.status().is_running:
            print(
                "[wuzapi] skip: sidecar not running (run `obscura whatsapp install` "
                "then `obscura whatsapp link`)",
                flush=True,
            )
            return
    except Exception as exc:
        print(f"[wuzapi] skip: lifecycle probe failed: {exc!r}", flush=True)
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

    # Quick port-bind probe before spinning up uvicorn. If the standalone
    # whatsapp-daemon LaunchAgent already owns the port, we don't try to
    # second-bind — the daemon does the bridging and we just rely on its
    # UDS broadcasts. This keeps the REPL boot quiet in the common case
    # where users run the daemon LaunchAgent separately.
    import socket
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", webhook_port))
    except OSError:
        print(
            f"[wuzapi] skip: port {webhook_port} already bound (likely a "
            f"standalone whatsapp-daemon); relying on its UDS broadcasts",
            flush=True,
        )
        probe.close()
        return
    probe.close()

    try:
        from obscura.integrations.whatsapp.wuzapi.service import wuzapi_service

        cm = wuzapi_service(webhook_port=webhook_port)
        await cm.__aenter__()
    except Exception as exc:
        print(
            f"[wuzapi] failed to start webhook receiver: {exc!r}",
            flush=True,
        )
        return

    session.register_resource(cm, name="wuzapi_service")
    print(
        f"[wuzapi] bridge live on 127.0.0.1:{webhook_port}/inbound",
        flush=True,
    )
