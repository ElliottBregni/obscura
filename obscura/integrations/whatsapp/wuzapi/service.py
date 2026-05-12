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
import dataclasses
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
DEFAULT_DEBOUNCE_WINDOW_S: Final[float] = 2.5
DEFAULT_REPLY_MIN_GAP_S: Final[float] = 3.0
"""Minimum time between consecutive replies to the same sender."""
DEFAULT_REPLY_MAX_PER_HOUR: Final[int] = 20
"""Hard cap on replies-per-hour per sender. Bounds runaway loops even if
echo detection has bugs."""
"""Default per-sender debounce window. Bursts within this window coalesce
into a single PlatformMessage with newline-joined text. Tuned for human
typing cadence — under 1s feels jumpy, over 5s feels laggy."""

AutoResponder = Callable[[PlatformMessage, "WuzapiAdapter"], Awaitable[None]]
"""Optional callable that produces and dispatches a reply for each inbound msg."""


# ---------------------------------------------------------------------------
# Per-sender debounce buffer
# ---------------------------------------------------------------------------


class _DebouncedDispatcher:
    """Coalesces rapid bursts of inbound messages per sender.

    Each new message resets the per-sender timer; when the timer expires
    we dispatch a single PlatformMessage whose text is the newline-joined
    concatenation of all buffered messages. Metadata + ids inherit from
    the *last* message in the burst (so message_id, timestamp etc.
    reference the most recent activity).

    Single-message conversations pay the debounce_window_s latency cost
    (default 2.5s) — that's the trade we make to handle bursts smoothly.
    """

    def __init__(
        self,
        on_flush: Callable[[PlatformMessage], Awaitable[None]],
        *,
        window_s: float = DEFAULT_DEBOUNCE_WINDOW_S,
    ) -> None:
        self._on_flush = on_flush
        self._window_s = window_s
        self._buffers: dict[str, list[PlatformMessage]] = {}
        self._timers: dict[str, asyncio.Task[None]] = {}

    async def feed(self, msg: PlatformMessage) -> None:
        key = msg.sender_id
        self._buffers.setdefault(key, []).append(msg)
        existing = self._timers.get(key)
        if existing is not None and not existing.done():
            existing.cancel()
        self._timers[key] = asyncio.create_task(self._wait_and_flush(key))

    async def _wait_and_flush(self, key: str) -> None:
        try:
            await asyncio.sleep(self._window_s)
        except asyncio.CancelledError:
            return
        msgs = self._buffers.pop(key, [])
        self._timers.pop(key, None)
        if not msgs:
            return
        latest = msgs[-1]
        if len(msgs) == 1:
            coalesced = latest
        else:
            combined_text = "\n".join(m.text for m in msgs if m.text.strip())
            coalesced = dataclasses.replace(latest, text=combined_text)
            print(
                f"[wuzapi debounce] coalesced {len(msgs)} msgs from "
                f"{key} into one ({len(combined_text)} chars)",
                flush=True,
            )
        try:
            await self._on_flush(coalesced)
        except Exception:
            logger.exception("debounce flush handler raised")


# ---------------------------------------------------------------------------
# PlatformMessage → ChannelMessage adapter
# ---------------------------------------------------------------------------


class _ReplyRateLimit:
    """Per-sender outbound rate limit. Hard backstop against runaway loops.

    Two layered limits:

    * ``min_gap_s``: minimum seconds between consecutive replies to the
      same sender. Default 3s — fast enough for natural back-and-forth,
      slow enough that a feedback loop can't fire 20 times per second.
    * ``max_per_hour``: hard ceiling on replies to a single sender per
      rolling hour. Default 20 — well above normal conversation pace,
      orders of magnitude below a runaway loop.

    When a reply is rate-limited, we log and drop it — the caller treats
    that as a "no reply this turn" rather than a retry. Avoids piling up
    deferred replies in memory.
    """

    def __init__(
        self,
        *,
        min_gap_s: float = DEFAULT_REPLY_MIN_GAP_S,
        max_per_hour: int = DEFAULT_REPLY_MAX_PER_HOUR,
    ) -> None:
        self._min_gap_s = min_gap_s
        self._max_per_hour = max_per_hour
        self._history: dict[str, list[float]] = {}

    def allow(self, sender: str) -> tuple[bool, str]:
        """Return (allowed, reason). On True, records the send timestamp."""
        import time as _time
        now = _time.time()
        h = self._history.setdefault(sender, [])
        # Prune anything older than an hour
        h[:] = [ts for ts in h if now - ts < 3600.0]
        if h and (now - h[-1]) < self._min_gap_s:
            gap = now - h[-1]
            return False, f"min-gap {self._min_gap_s:g}s not elapsed (only {gap:.2f}s)"
        if len(h) >= self._max_per_hour:
            return False, f"hourly cap reached ({self._max_per_hour})"
        h.append(now)
        return True, ""


def _to_channel_message(
    msg: PlatformMessage,
    adapter: WuzapiAdapter,
    rate_limit: _ReplyRateLimit,
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
        # Rate-limit BEFORE adapter.send so we don't even mark the text
        # as "recently sent" when it's being dropped. The hourly cap +
        # min-gap together make runaway loops impossible to amplify.
        allowed, reason = rate_limit.allow(reply_target)
        if not allowed:
            print(
                f"[wuzapi rate-limit] dropping reply to {reply_target}: {reason}",
                flush=True,
            )
            return False
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
    debounce_window_s: float = DEFAULT_DEBOUNCE_WINDOW_S,
    reply_min_gap_s: float = DEFAULT_REPLY_MIN_GAP_S,
    reply_max_per_hour: int = DEFAULT_REPLY_MAX_PER_HOUR,
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

    rate_limit = _ReplyRateLimit(
        min_gap_s=reply_min_gap_s, max_per_hour=reply_max_per_hour,
    )

    async def _flush(msg: PlatformMessage) -> None:
        delivered = push_channel_message(
            _to_channel_message(msg, adapter, rate_limit)
        )
        print(
            f"[wuzapi → REPL] from={msg.sender_id} text={msg.text[:80]!r} delivered={delivered}",
            flush=True,
        )
        logger.info(
            "wuzapi → REPL: from=%s text=%r delivered=%s",
            msg.sender_id, msg.text[:80], delivered,
        )
        if auto_responder is not None:
            asyncio.create_task(_safe_auto_respond(auto_responder, msg, adapter))

    debounced = _DebouncedDispatcher(_flush, window_s=debounce_window_s)

    async def on_event(env: WuzapiWebhookEnvelope) -> None:
        msg = adapter.handle_event(env)
        if msg is None:
            print(f"[wuzapi] dropped event type={env.type!r} (non-Message or echo)", flush=True)
            return
        # Belt-and-suspenders: the adapter already drops blank text but in
        # case downstream parsing slips through, double-check here so we
        # never debounce empty content.
        if not msg.text.strip():
            print(f"[wuzapi] dropped blank-text msg from {msg.sender_id}", flush=True)
            return
        await debounced.feed(msg)

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
