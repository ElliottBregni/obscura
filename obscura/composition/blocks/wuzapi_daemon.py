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

import asyncio
import logging
import tomllib
from typing import TYPE_CHECKING, Any, cast

from obscura.core.paths import resolve_obscura_home

if TYPE_CHECKING:
    from obscura.composition.session import AgentSession, SessionConfig

logger = logging.getLogger(__name__)

_DEFAULT_WEBHOOK_PORT = 18794
_PROMOTION_PROBE_INTERVAL_S = 5.0


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


def _cleanup_dead_session_state() -> int:
    """Remove session locks/sockets whose PIDs no longer exist.

    Orphan REPL state accumulates when prompt_toolkit crashes mid-startup
    or when the user opens multiple REPL terminals and closes them
    uncleanly. Each leftover lock/socket pretends to be a "live peer"
    until cleaned. Returns the count of artifacts removed.
    """
    import json
    import os
    home = resolve_obscura_home()
    sessions_dir = home / "sessions"
    sockets_dir = home / "sockets"
    if not sessions_dir.is_dir():
        return 0

    removed = 0
    live_session_ids: set[str] = set()
    for lock in sessions_dir.glob("*.lock"):
        try:
            data = json.loads(lock.read_text())
            pid = int(data.get("pid", 0) or 0)
            sid = str(data.get("session_id") or lock.stem)
        except Exception:
            lock.unlink(missing_ok=True)
            removed += 1
            continue
        if pid <= 0:
            lock.unlink(missing_ok=True)
            removed += 1
            continue
        try:
            os.kill(pid, 0)
            live_session_ids.add(sid)
        except (ProcessLookupError, PermissionError):
            lock.unlink(missing_ok=True)
            removed += 1

    if sockets_dir.is_dir():
        for sock in sockets_dir.glob("*.sock"):
            if sock.stem not in live_session_ids:
                sock.unlink(missing_ok=True)
                removed += 1
    return removed


async def _try_acquire_port_and_start_service(
    session: AgentSession,
    webhook_port: int,
) -> bool:
    """Attempt to bind ``webhook_port`` and start the wuzapi service.

    Returns True if we acquired the port AND the service entered cleanly
    (i.e. this REPL is now the inbound owner). False means the port was
    taken or service startup failed — caller stays in peer mode.

    The probe→service-start sequence has a microsecond race window where
    another peer could grab the port between our ``probe.close()`` and
    uvicorn's bind. If that happens we catch the OSError and return False,
    leaving us as a peer for the next promotion cycle.
    """
    import socket
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", webhook_port))
    except OSError:
        return False
    finally:
        probe.close()

    try:
        from obscura.integrations.whatsapp.wuzapi.service import wuzapi_service
        cm = wuzapi_service(
            webhook_port=webhook_port,
            session_id=session.session_id,
        )
        await cm.__aenter__()
    except Exception as exc:
        print(
            f"[wuzapi] port probe succeeded but service start failed: {exc!r}",
            flush=True,
        )
        return False

    session.register_resource(cm, name="wuzapi_service")
    return True


async def _promotion_watcher(
    session: AgentSession,
    webhook_port: int,
) -> None:
    """Poll ``webhook_port`` and auto-promote this REPL to owner when free.

    Sleeps :data:`_PROMOTION_PROBE_INTERVAL_S` between probes. Exits as
    soon as a probe succeeds and the service starts — no further work
    needed once we own the port. Cancellation during ``asyncio.sleep`` is
    a clean exit (caught and re-raised by the sleep itself, then we
    return). Any other exception during a probe iteration is logged and
    swallowed so transient failures don't kill the watcher.
    """
    while True:
        try:
            await asyncio.sleep(_PROMOTION_PROBE_INTERVAL_S)
        except asyncio.CancelledError:
            return
        try:
            if await _try_acquire_port_and_start_service(session, webhook_port):
                print(
                    f"[wuzapi] AUTO-PROMOTED — bridge live on "
                    f"127.0.0.1:{webhook_port}/inbound "
                    f"(previous owner exited)",
                    flush=True,
                )
                return
        except asyncio.CancelledError:
            return
        except Exception:
            logger.debug(
                "wuzapi promotion watcher: probe iteration failed",
                exc_info=True,
            )


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

    # Sweep orphan session locks/sockets before anything else — accumulated
    # dead state misleads discover_peers() (now self-filters but still
    # reports dead peers) and bloats every sweep with no-op stats.
    cleaned = _cleanup_dead_session_state()
    if cleaned > 0:
        print(f"[wuzapi] cleaned {cleaned} stale session lock(s)/socket(s)", flush=True)

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

    # Try to acquire the inbound port right now. If another REPL already
    # owns it, we become a *peer* — receive inbound via UDS fanout from
    # the owner — and spawn a promotion watcher that will auto-claim the
    # port the moment the current owner exits. This is what makes
    # "multi-REPL with no startup/shutdown hassle" work: any order of
    # boots is fine, any order of exits is fine, the bridge always lives
    # on whichever REPL happens to be running.
    if await _try_acquire_port_and_start_service(session, webhook_port):
        print(
            f"[wuzapi] OWNER — bridge live on 127.0.0.1:{webhook_port}/inbound "
            f"(inbound webhooks land here)",
            flush=True,
        )
        return

    print(
        f"[wuzapi] PEER — port {webhook_port} owned by another REPL; "
        f"receiving via UDS fanout, will auto-promote if owner exits",
        flush=True,
    )
    watcher = asyncio.create_task(
        _promotion_watcher(session, webhook_port),
        name="wuzapi-promotion-watcher",
    )
    session.register_resource(watcher, name="wuzapi_promotion_watcher")
