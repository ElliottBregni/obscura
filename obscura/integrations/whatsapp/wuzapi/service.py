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
from collections.abc import AsyncGenerator
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


# ---------------------------------------------------------------------------
# PlatformMessage → ChannelMessage adapter
# ---------------------------------------------------------------------------


def _to_channel_message(
    msg: PlatformMessage,
    adapter: WuzapiAdapter,
) -> ChannelMessage:
    """Repack a platform message into the inject-queue's shape.

    The ``reply_fn`` closure captures the adapter so the agent's reply
    routes back through wuzapi to the same conversation. We strip the
    leading ``+`` from the sender JID before handing to ``adapter.send``;
    the adapter normalises again internally.
    """

    async def reply_fn(text: str) -> bool:
        return await adapter.send(msg.sender_id, text)

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
            return
        # push_channel_message is synchronous — it queues + UDS broadcasts
        delivered = push_channel_message(_to_channel_message(msg, adapter))
        logger.info(
            "wuzapi → REPL: from=%s text=%r delivered=%s",
            msg.sender_id, msg.text[:80], delivered,
        )

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


__all__ = [
    "DEFAULT_WEBHOOK_HOST",
    "DEFAULT_WEBHOOK_PORT",
    "wuzapi_service",
]
