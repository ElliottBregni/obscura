"""Long-running service that bridges wuzapi inbound → obscura REPL inbox.

Glue between :func:`build_webhook_app` (HTTP receiver) and
:mod:`channel_inject` (REPL inbox queue + UDS fan-out).

Lifecycle::

    async with wuzapi_service(port=18794):
        ...

While the context is alive, an HTTP server listens on the configured
loopback port, parses wuzapi POSTs into typed envelopes, hands each to
:class:`WuzapiAdapter.handle_event`, and forwards the resulting
:class:`PlatformMessage` into ``channel_inject._queue`` via
``push_channel_message``.

The service is **clean opt-in**: nothing here runs unless explicitly
constructed. Importing this module has zero side effects.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import Final

import uvicorn

from obscura.integrations.messaging.channel_inject import (
    ChannelMessage,
    push_channel_message,
)
from obscura.integrations.messaging.models import PlatformMessage
from obscura.integrations.whatsapp.wuzapi.adapter import WuzapiAdapter
from obscura.integrations.whatsapp.wuzapi.client import WuzapiClient
from obscura.integrations.whatsapp.wuzapi.models import WuzapiWebhookEnvelope
from obscura.integrations.whatsapp.wuzapi.setup import load_user_token
from obscura.integrations.whatsapp.wuzapi.webhook import build_webhook_app

logger = logging.getLogger(__name__)

DEFAULT_WEBHOOK_HOST: Final[str] = "127.0.0.1"
DEFAULT_WEBHOOK_PORT: Final[int] = 18794

AutoResponder = Callable[[PlatformMessage, "WuzapiAdapter"], Awaitable[None]]
"""Optional callable that produces and dispatches a reply for each inbound msg."""


# ---------------------------------------------------------------------------
# PlatformMessage → ChannelMessage adapter
# ---------------------------------------------------------------------------


def _to_channel_message(
    msg: PlatformMessage,
    adapter: WuzapiAdapter,
) -> ChannelMessage:
    """Repack a platform message into the inject-queue's shape.

    The ``reply_fn`` closure captures the adapter so the agent's reply
    routes back into the **same WhatsApp thread** the inbound came from.
    We use the *full* Chat JID (preserved in ``metadata['jid_chat']``)
    rather than the stripped ``sender_id`` — this correctly handles:

      * DMs to a phone contact     → Chat == Sender == phone JID
      * Group threads              → Chat == group JID (not the sender)
      * Self-chats from your phone → Chat == your own LID/phone JID
        (sender_id alone would be a bare LID number that wuzapi can't
        route as a phone number)
    """
    reply_target = str(msg.metadata.get("jid_chat") or msg.sender_id)

    async def reply_fn(text: str) -> bool:
        return await adapter.send(reply_target, text)

    return ChannelMessage(
        platform=msg.platform,
        sender_id=msg.sender_id,
        text=msg.text,
        reply_fn=reply_fn,
        display_name=str(msg.metadata.get("push_name") or ""),
        account_id=msg.account_id,
    )


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


@asynccontextmanager
async def wuzapi_service(
    *,
    wuzapi_base_url: str = "http://127.0.0.1:18793",
    webhook_host: str = DEFAULT_WEBHOOK_HOST,
    webhook_port: int = DEFAULT_WEBHOOK_PORT,
    account_id: str = "default",
    auto_responder: AutoResponder | None = None,
) -> AsyncGenerator[None]:
    """Run the inbound bridge for the lifetime of the ``async with`` block.

    Composes:

    * :class:`WuzapiClient` — outbound (and session probes for ``start()``)
    * :class:`WuzapiAdapter` — wire→PlatformMessage conversion
    * :func:`build_webhook_app` — Starlette receiver
    * ``uvicorn.Server`` — HTTP host on the loopback port
    * :func:`push_channel_message` — drains into ``channel_inject._queue``
      and fans out via UDS to any peer REPL processes
    """
    token = load_user_token()
    client = WuzapiClient(token=token, base_url=wuzapi_base_url)
    adapter = WuzapiAdapter(client, account_id=account_id)
    await adapter.start()

    async def on_event(env: WuzapiWebhookEnvelope) -> None:
        msg = adapter.handle_event(env)
        if msg is None:
            print(f"[wuzapi] dropped event type={env.type!r} (non-Message or echo)", flush=True)
            return
        delivered = push_channel_message(_to_channel_message(msg, adapter))
        print(
            f"[wuzapi → REPL] from={msg.sender_id} text={msg.text[:80]!r} delivered={delivered}",
            flush=True,
        )
        logger.info(
            "wuzapi → REPL: from=%s text=%r delivered=%s",
            msg.sender_id, msg.text[:80], delivered,
        )
        # If auto-responder is configured, dispatch the LLM call as a fire-
        # and-forget task. Failures are isolated (logged + swallowed by the
        # _safe_dispatch wrapper above) so they never reach the webhook ACK.
        if auto_responder is not None:
            asyncio.create_task(_safe_auto_respond(auto_responder, msg, adapter))

    app = build_webhook_app(on_event=on_event)
    config = uvicorn.Config(
        app, host=webhook_host, port=webhook_port,
        log_level="warning", access_log=False,
    )
    server = uvicorn.Server(config)

    serve_task = asyncio.create_task(server.serve())
    logger.info("wuzapi service: webhook listening on %s:%d",
                webhook_host, webhook_port)
    try:
        yield
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(serve_task, timeout=5.0)
        except (TimeoutError, asyncio.CancelledError):
            serve_task.cancel()
        await client.aclose()
        logger.info("wuzapi service: shut down")


async def _safe_auto_respond(
    responder: AutoResponder,
    msg: PlatformMessage,
    adapter: WuzapiAdapter,
) -> None:
    """Run the auto-responder with exception isolation + visible logging."""
    print(
        f"[wuzapi auto-respond] starting for from={msg.sender_id} text={msg.text[:60]!r}",
        flush=True,
    )
    try:
        await responder(msg, adapter)
    except Exception as exc:
        print(
            f"[wuzapi auto-respond] FAILED for from={msg.sender_id}: {exc!r}",
            flush=True,
        )
        logger.exception("wuzapi auto-respond failed for %s", msg.sender_id)
    else:
        print(f"[wuzapi auto-respond] done for from={msg.sender_id}", flush=True)


__all__ = [
    "AutoResponder",
    "DEFAULT_WEBHOOK_HOST",
    "DEFAULT_WEBHOOK_PORT",
    "wuzapi_service",
]
