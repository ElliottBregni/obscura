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
from typing import Any, Final, cast

import uvicorn

from obscura.integrations.messaging.channel_inject import (
    ChannelMessage,
    push_channel_message,
)
from obscura.integrations.messaging.command_acl import is_reply_allowed
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
DEFAULT_TYPING_REFRESH_INTERVAL_S: Final[float] = 8.0
"""Re-send ``composing`` every 8s (under WhatsApp's ~10s timeout).
Below 10s gives a comfortable margin; below 5s wastes API calls."""
DEFAULT_TYPING_MAX_DURATION_S: Final[float] = 60.0
"""Hard cap on how long a single typing indicator stays alive. If the
agent never calls reply_fn (e.g. decides not to reply), the bubble
auto-clears after this — avoids a forever-typing bubble."""
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


class _TypingTracker:
    """Per-recipient WhatsApp typing indicator with keepalive + auto-clear.

    Lifecycle::

        await tracker.start(jid)   # sends composing immediately
        # ... agent generates a response ...
        await tracker.stop(jid)    # cancels keepalive + sends paused

    WhatsApp clears the indicator after ~10s of silence, so we refresh
    ``composing`` every :data:`DEFAULT_TYPING_REFRESH_INTERVAL_S` seconds
    until ``stop`` is called or :data:`DEFAULT_TYPING_MAX_DURATION_S` is
    hit. All presence errors are swallowed — the typing bubble is a UX
    nicety and must never block or fail a real reply.

    Idempotency:
    * ``start(jid)`` while a keepalive is already running for ``jid`` is
      a no-op (won't double-fire the initial composing).
    * ``stop(jid)`` when no keepalive exists still sends ``paused``
      (clears any stale indicator from a previous session).
    """

    def __init__(
        self,
        client: WuzapiClient,
        *,
        refresh_interval_s: float = DEFAULT_TYPING_REFRESH_INTERVAL_S,
        max_duration_s: float = DEFAULT_TYPING_MAX_DURATION_S,
    ) -> None:
        self._client = client
        self._refresh_interval_s = refresh_interval_s
        self._max_duration_s = max_duration_s
        self._tasks: dict[str, asyncio.Task[None]] = {}

    async def start(self, jid: str) -> None:
        existing = self._tasks.get(jid)
        if existing is not None and not existing.done():
            return
        try:
            await self._client.set_chat_presence(jid, state="composing")
        except Exception:
            logger.debug(
                "typing: initial composing failed for %s",
                jid,
                exc_info=True,
            )
        self._tasks[jid] = asyncio.create_task(
            self._keepalive(jid),
            name=f"wuzapi-typing-{jid[:32]}",
        )

    async def stop(self, jid: str) -> None:
        task = self._tasks.pop(jid, None)
        if task is not None and not task.done():
            task.cancel()
        try:
            await self._client.set_chat_presence(jid, state="paused")
        except Exception:
            logger.debug(
                "typing: paused failed for %s",
                jid,
                exc_info=True,
            )

    async def _keepalive(self, jid: str) -> None:
        loop = asyncio.get_event_loop()
        deadline = loop.time() + self._max_duration_s
        try:
            while loop.time() < deadline:
                try:
                    await asyncio.sleep(self._refresh_interval_s)
                except asyncio.CancelledError:
                    return
                try:
                    await self._client.set_chat_presence(
                        jid,
                        state="composing",
                    )
                except Exception:
                    logger.debug(
                        "typing: refresh failed for %s",
                        jid,
                        exc_info=True,
                    )
            try:
                await self._client.set_chat_presence(jid, state="paused")
            except Exception:
                logger.debug(
                    "typing: auto-clear failed for %s",
                    jid,
                    exc_info=True,
                )
        finally:
            self._tasks.pop(jid, None)

    def cancel_all(self) -> None:
        for task in list(self._tasks.values()):
            if not task.done():
                task.cancel()
        self._tasks.clear()


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
    reply_target: str,
    adapter: WuzapiAdapter,
    rate_limit: _ReplyRateLimit,
    typing: _TypingTracker,
) -> ChannelMessage:
    """Repack a platform message into the inject-queue's shape.

    The ``reply_fn`` closure captures the adapter so the agent's reply
    routes back into the **same WhatsApp thread** the inbound came from.
    ``reply_target`` (the full Chat JID, preserved in
    ``metadata['jid_chat']`` upstream) is computed by the caller so the
    typing tracker can use the same target. Using the Chat JID rather
    than the stripped ``sender_id`` correctly handles:

      * DMs to a phone contact     → Chat == Sender == phone JID
      * Group threads              → Chat == group JID (not the sender)
      * Self-chats from your phone → Chat == your own LID/phone JID
        (sender_id alone would be a bare LID number that wuzapi can't
        route as a phone number)

    The reply_fn always stops the typing tracker on exit (success,
    rate-limit drop, or exception). Without a try/finally here, a stuck
    typing bubble would shadow real conversation state.
    """

    async def reply_fn(text: str) -> bool:
        # Rate-limit BEFORE adapter.send so we don't even mark the text
        # as "recently sent" when it's being dropped. The hourly cap +
        # min-gap together make runaway loops impossible to amplify.
        try:
            allowed, reason = rate_limit.allow(reply_target)
            if not allowed:
                print(
                    f"[wuzapi rate-limit] dropping reply to {reply_target}: {reason}",
                    flush=True,
                )
                return False
            return await adapter.send(reply_target, text)
        finally:
            await typing.stop(reply_target)

    async def progress_fn(text: str) -> bool:
        """Out-of-band 'still working' ping while the agent processes.

        Bypasses the rate limit by construction (never calls
        ``rate_limit.allow``) so the hourly cap is reserved exclusively
        for final replies. Does NOT call ``typing.stop`` — the
        keepalive in ``_TypingTracker`` will re-send ``composing`` on
        its next tick (within ~8s), so the bubble naturally re-appears
        between pings. Errors are swallowed: a failed progress ping
        must never block the final reply path.
        """
        try:
            return await adapter.send(reply_target, text)
        except Exception:
            logger.debug(
                "progress ping send failed for %s",
                reply_target,
                exc_info=True,
            )
            return False

    return ChannelMessage(
        platform=msg.platform,
        sender_id=msg.sender_id,
        text=msg.text,
        reply_fn=reply_fn,
        progress_fn=progress_fn,
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
    session_id: str = "",
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
        min_gap_s=reply_min_gap_s,
        max_per_hour=reply_max_per_hour,
    )
    typing = _TypingTracker(client)

    # Cache the linked device's own phone-JID digits for self-chat
    # detection. WhatsApp Multi-Device can emit self-chat events with
    # ANY combination of LID/phone forms for Chat and Sender, so we
    # need a third reference point ("this is the linked device") in
    # addition to the chat==sender structural check. Without this, a
    # self-chat with Chat=phone/Sender=LID is mis-classified as
    # "user typing in DM with phone-number-X" and dropped.
    self_jid_digits: str = ""
    try:
        status = await client.session_status()
        if status.jid:
            self_jid_digits = _digits_only(_strip_device_suffix(status.jid))
    except Exception:
        logger.debug(
            "wuzapi: failed to fetch self JID for ACL self-chat detection",
            exc_info=True,
        )

    async def _flush(msg: PlatformMessage) -> None:
        reply_target = str(msg.metadata.get("jid_chat") or msg.sender_id)
        # Show "typing..." on the recipient's phone while the agent is
        # composing. Starts now (after debounce settles), stops in
        # reply_fn after adapter.send completes. Hard-capped at
        # DEFAULT_TYPING_MAX_DURATION_S so an agent that never replies
        # doesn't leave a dangling bubble.
        await typing.start(reply_target)
        delivered = push_channel_message(
            _to_channel_message(msg, reply_target, adapter, rate_limit, typing)
        )
        print(
            f"[wuzapi → REPL] from={msg.sender_id} text={msg.text[:80]!r} delivered={delivered}",
            flush=True,
        )
        logger.info(
            "wuzapi → REPL: from=%s text=%r delivered=%s",
            msg.sender_id,
            msg.text[:80],
            delivered,
        )
        if auto_responder is not None:
            asyncio.create_task(_safe_auto_respond(auto_responder, msg, adapter))

    debounced = _DebouncedDispatcher(_flush, window_s=debounce_window_s)

    async def on_event(env: WuzapiWebhookEnvelope) -> None:
        msg = adapter.handle_event(env)
        if msg is None:
            # handle_event returns None for several distinct reasons: a
            # non-Message envelope type, a malformed Message payload, an
            # ID-matched or text-matched echo of our own outbound, or a
            # Message whose extractable text is blank (now rare since
            # media variants synthesize markers, but reactions/receipts
            # still fall here). DEBUG log inside the adapter has the
            # specific cause.
            print(
                f"[wuzapi] adapter dropped event type={env.type!r} "
                f"(non-Message, malformed, echo, or no extractable content)",
                flush=True,
            )
            return
        # If this message had downloadable media, save it to disk and
        # rewrite the text to include the path. Two paths:
        #
        #   FAST: wuzapi's processMedia ran server-side and embedded
        #         the bytes as envelope.base64 — the adapter copied
        #         it into metadata["inline_media_b64"]. Just decode
        #         + save. No HTTP roundtrip, can't fail on a
        #         single-use CDN URL.
        #   SLOW: fall back to /chat/download* via media_payload.
        #         Useful when wuzapi is in S3-only mode or skipMedia
        #         is true; otherwise typically redundant or fails.
        #
        # Failures at both layers fall back to the synthesized
        # "[image]"/"[video]"/etc marker — message still routes, just
        # without the file.
        media_payload = msg.metadata.get("media_payload")
        if isinstance(media_payload, dict):
            payload_dict = cast("dict[str, Any]", media_payload)
            message_id = str(msg.metadata.get("wuzapi_message_id") or "")
            kind = str(payload_dict.get("kind") or "media")
            saved_path: str | None = None

            inline_b64 = msg.metadata.get("inline_media_b64")
            if isinstance(inline_b64, str) and inline_b64:
                inline_mimetype = str(
                    msg.metadata.get("inline_media_mimetype")
                    or payload_dict.get("mimetype")
                    or "",
                )
                saved_path = _save_inline_media(
                    inline_b64,
                    inline_mimetype,
                    message_id,
                )
                if saved_path:
                    print(
                        f"[wuzapi] saved inline {kind} bytes to {saved_path} "
                        f"({len(inline_b64)} b64 chars)",
                        flush=True,
                    )
                else:
                    print(
                        f"[wuzapi] inline {kind} decode/save failed; "
                        f"falling back to /chat/download*",
                        flush=True,
                    )

            if saved_path is None:
                saved_path = await _download_and_save_media(
                    client,
                    payload_dict,
                    message_id,
                )
                if saved_path:
                    print(
                        f"[wuzapi] downloaded {kind} to {saved_path}",
                        flush=True,
                    )

            if saved_path:
                # Replace the synthesized marker with "[<kind> at <path>]".
                # Preserves any "caption: ..." suffix already there.
                marker = str(payload_dict.get("marker") or "")
                if marker and marker in msg.text:
                    marker_inner = marker.strip("[]")
                    new_text = msg.text.replace(
                        marker,
                        f"[{marker_inner} at {saved_path}]",
                        1,
                    )
                    msg = dataclasses.replace(msg, text=new_text)
        # Belt-and-suspenders: the adapter already drops blank text but in
        # case downstream parsing slips through, double-check here so we
        # never debounce empty content.
        if not msg.text.strip():
            print(f"[wuzapi] dropped blank-text msg from {msg.sender_id}", flush=True)
            return
        chat_jid = str(msg.metadata.get("jid_chat") or "")
        is_from_me = bool(msg.metadata.get("is_from_me"))
        allow, reason = _should_route_inbound(
            sender_id=msg.sender_id,
            chat_jid=chat_jid,
            is_from_me=is_from_me,
            self_jid_digits=self_jid_digits,
        )
        if not allow:
            print(f"[wuzapi] dropped: {reason}", flush=True)
            return
        await debounced.feed(msg)

    app = build_webhook_app(on_event=on_event)
    config = uvicorn.Config(
        app,
        host=webhook_host,
        port=webhook_port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)

    serve_task = asyncio.create_task(server.serve())
    logger.info(
        "wuzapi service: webhook listening on %s:%d", webhook_host, webhook_port
    )

    # Announce this session's id to the linked device's self-chat so
    # the user can see which obscura REPL is currently the bridge
    # OWNER. Fire-and-forget — the startup path shouldn't block on a
    # cosmetic message, and any failure (wuzapi not linked, network
    # blip, no JID returned) is swallowed inside _announce_session.
    if session_id:
        asyncio.create_task(
            _announce_session(client, adapter, session_id),
            name="wuzapi-session-announce",
        )

    try:
        yield
    finally:
        # Cancel typing keepalives first so they don't fire mid-shutdown
        # and race the client.aclose() below.
        typing.cancel_all()
        server.should_exit = True
        try:
            await asyncio.wait_for(serve_task, timeout=5.0)
        except (TimeoutError, asyncio.CancelledError):
            serve_task.cancel()
        await client.aclose()
        logger.info("wuzapi service: shut down")


def _digits_only(s: str) -> str:
    """Extract digits + strip US country-code prefix for ACL comparisons.

    Mirrors the normalization in command_acl so the self-chat detection
    here and the allowlist lookup there agree on what "the same number"
    means. Stripping the leading ``1`` matches a US 10-digit number
    against an 11-digit-with-country-code form.
    """
    digits = "".join(c for c in s if c.isdigit())
    if len(digits) >= 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits


def _should_route_inbound(
    *,
    sender_id: str,
    chat_jid: str,
    is_from_me: bool,
    self_jid_digits: str = "",
) -> tuple[bool, str]:
    """Decide whether an inbound WhatsApp message reaches the REPL.

    Returns ``(allow, reason)``. On False, ``reason`` is a human-readable
    diagnostic; on True, ``reason`` is the route classification.

    Rules, in precedence order:

    1. **Group chat** (``chat_jid`` ends ``@g.us``): drop unless the
       full group JID is in ``reply_allowlist``.
    2. **Self-chat** (``IsFromMe=true`` AND any of three identity
       matches holds): always allow. The three matches are:

         * ``chat_digits == sender_digits`` — whatsmeow used the same
           form for both ends (Chat=LID/Sender=LID OR
           Chat=phone/Sender=phone).
         * ``chat_digits == self_jid_digits`` — chat matches the
           linked device's known phone JID. Catches the asymmetric
           Chat=phone/Sender=LID case the user hit in production.
         * ``sender_digits == self_jid_digits`` — sender matches the
           linked device's known phone JID. Catches Chat=LID/Sender=phone.

       The triple check is necessary because WhatsApp Multi-Device can
       route self-chats with any combination of LID and phone forms
       for Chat and Sender. We can't predict which combination
       whatsmeow will emit, so having a third reference point (the
       linked device's JID, cached at service startup) ensures every
       combination still resolves correctly.
    3. **User typing in DM with someone else** (``IsFromMe=true`` AND
       no self-chat match): drop. The agent must NOT intercept the
       user's outbound to other contacts.
    4. **Inbound from another sender** (``IsFromMe=false``): check
       ``sender_id`` against ``reply_allowlist``; allow only if listed.
    """
    chat_raw = chat_jid.split("@", 1)[0] if "@" in chat_jid else chat_jid
    chat_digits = _digits_only(chat_raw)
    sender_digits = _digits_only(sender_id)

    if chat_jid.endswith("@g.us"):
        if is_reply_allowed("whatsapp", chat_jid):
            return True, "allowlisted group"
        return False, (
            f"group msg in {chat_jid} (add full group JID to "
            f"[messaging.whatsapp].reply_allowlist to enable)"
        )
    if is_from_me:
        if chat_digits and chat_digits == sender_digits:
            return True, "self-chat (chat==sender)"
        if self_jid_digits and chat_digits == self_jid_digits:
            return True, "self-chat (chat matches linked device)"
        if self_jid_digits and sender_digits == self_jid_digits:
            return True, "self-chat (sender matches linked device)"
        return False, (
            f"user-typed msg in DM with {chat_raw} "
            f"(IsFromMe=true but no self-chat match — agent must "
            f"not intercept user's outbound to others)"
        )
    if is_reply_allowed("whatsapp", sender_id):
        return True, "allowlisted sender"
    return False, (
        f"msg from non-allowlisted sender {sender_id} "
        f"(add to [messaging.whatsapp].reply_allowlist to enable)"
    )


def _save_inline_media(
    b64_data: str,
    mimetype: str,
    message_id: str,
) -> str | None:
    """Fast path: decode wuzapi's server-side-delivered base64 and save.

    wuzapi's processMedia runs whatsmeow.Download() inside the
    sidecar before firing the webhook, then base64-encodes the bytes
    into the envelope's top-level ``base64`` field. We use those
    bytes directly — much faster than calling /chat/download* and
    avoids the "single-use WhatsApp CDN URL" problem that breaks
    re-download attempts after processMedia has already consumed
    the URL once.

    Returns the saved path on success, ``None`` if decoding/saving
    fails (caller can then fall back to /chat/download*).
    """
    import base64 as _b64

    if not b64_data:
        return None
    try:
        data = _b64.b64decode(b64_data)
    except Exception:
        logger.debug("inline media: base64 decode failed", exc_info=True)
        return None
    if not data:
        return None
    from obscura.integrations.messaging.media_store import save_inbound_media

    return save_inbound_media(
        platform="whatsapp",
        message_id=message_id or "media",
        data=data,
        mimetype=mimetype,
    )


async def _download_and_save_media(
    client: WuzapiClient,
    media_payload: dict[str, Any],
    message_id: str,
) -> str | None:
    """Download inbound media via wuzapi, save under the shared
    ``media_inbound/whatsapp/`` directory, and return the absolute path.

    Dispatches to the right wuzapi endpoint based on
    ``media_payload["kind"]`` — image, video, document, or audio. All
    four use the same request shape (:class:`WuzapiDownloadMediaRequest`).

    Returns ``None`` on any failure (download error, write error,
    unsupported kind). Failures are printed to stdout with the
    ``[wuzapi]`` prefix so they're visible during diagnosis — without
    visible failure breadcrumbs we couldn't tell whether the issue
    was the download (wuzapi/network), the save (filesystem), or
    upstream (no media_payload extracted). DEBUG log retains the full
    traceback.

    The saved path is what we surface to the agent: it gets a prompt
    like ``[image at /Users/.../media_inbound/whatsapp/abc.jpg]
    caption: foo`` and picks up the file via its existing file/vision
    tools.
    """
    from obscura.integrations.messaging.media_store import save_inbound_media
    from obscura.integrations.whatsapp.wuzapi.models import (
        WuzapiDownloadMediaRequest,
    )

    kind = str(media_payload.get("kind") or "")
    downloader = {
        "image": client.download_image,
        "video": client.download_video,
        "document": client.download_document,
        "audio": client.download_audio,
    }.get(kind)
    if downloader is None:
        print(
            f"[wuzapi] media download skipped: unknown kind {kind!r}",
            flush=True,
        )
        return None

    url = str(media_payload.get("url", ""))
    mimetype = str(media_payload.get("mimetype", ""))
    print(
        f"[wuzapi] downloading {kind} from wuzapi (mimetype={mimetype!r}, "
        f"url_present={bool(url)})",
        flush=True,
    )

    try:
        req = WuzapiDownloadMediaRequest(
            url=url,
            direct_path=str(media_payload.get("direct_path", "")),
            media_key=str(media_payload.get("media_key", "")),
            mimetype=mimetype,
            file_enc_sha256=str(media_payload.get("file_enc_sha256", "")),
            file_sha256=str(media_payload.get("file_sha256", "")),
            file_length=int(media_payload.get("file_length", 0) or 0),
        )
        data = await downloader(req)
    except Exception as exc:
        # Visible failure — likely causes:
        #   * wuzapi binary is older than the /chat/download* endpoints
        #     (404 / no route) — rebuild with `obscura whatsapp install`
        #   * WhatsApp CDN URL expired (uncommon for fresh messages)
        #   * WhatsApp session broken — `obscura whatsapp status`
        #   * mediaKey mismatch (decode/serialization bug)
        print(
            f"[wuzapi] {kind} download failed: {exc!r}",
            flush=True,
        )
        logger.debug("wuzapi: %s download exception", kind, exc_info=True)
        return None

    if not data:
        print(
            f"[wuzapi] {kind} download returned empty bytes (wuzapi may "
            f"have decoded but produced no payload)",
            flush=True,
        )
        return None

    saved_path = save_inbound_media(
        platform="whatsapp",
        message_id=message_id or kind,
        data=data,
        mimetype=mimetype,
    )
    if saved_path is None:
        print(
            f"[wuzapi] {kind} downloaded ({len(data)} bytes) but could "
            f"not be saved to disk — check ~/.obscura/media_inbound/ "
            f"permissions",
            flush=True,
        )
    return saved_path


def _strip_device_suffix(jid: str) -> str:
    """Strip the ``:<device>`` segment from a WhatsApp JID.

    whatsmeow's session_status returns the linked device JID like
    ``12316333624:14@s.whatsapp.net`` (where ``:14`` identifies the
    specific linked-device session). For outbound messages we want the
    bare JID ``12316333624@s.whatsapp.net`` so WhatsApp routes to all
    of the user's devices, not just the one wuzapi attached as.
    """
    if "@" not in jid:
        return jid
    phone_part, server = jid.split("@", 1)
    if ":" in phone_part:
        phone_part = phone_part.split(":", 1)[0]
    return f"{phone_part}@{server}"


async def _announce_session(
    client: WuzapiClient,
    adapter: WuzapiAdapter,
    session_id: str,
) -> None:
    """Send a one-time 'session connected' message to the self-chat.

    Looks up the linked device's own JID via wuzapi's session_status,
    strips the device suffix, and sends an announcement using the same
    adapter that handles normal outbound. Best-effort: any failure is
    logged at DEBUG and swallowed. Sending via adapter.send means the
    text is recorded in ``_recent_send_texts``, so when the message
    bounces back as an inbound webhook event (as it does for any
    outbound on linked-device WhatsApp) it gets caught by the
    text-match echo detection and dropped — no feedback loop.
    """
    try:
        status = await client.session_status()
    except Exception:
        logger.debug("session announce: status fetch failed", exc_info=True)
        return
    if not status.jid:
        logger.debug("session announce: no JID in status (not linked?)")
        return
    self_jid = _strip_device_suffix(status.jid)
    short_id = session_id[:8] if len(session_id) > 8 else session_id
    msg = (
        f"[obscura] session {short_id} connected as owner. "
        f"Default replier for this thread."
    )
    try:
        await adapter.send(self_jid, msg)
        print(
            f"[wuzapi] announced session {short_id} to {self_jid}",
            flush=True,
        )
    except Exception:
        logger.debug("session announce: send failed", exc_info=True)


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
