"""MessagePlatformAdapter implementation backed by the wuzapi sidecar.

This adapter is webhook-driven, not polling-driven. ``poll()`` always
returns an empty list — kept for protocol compatibility with the existing
``ChannelDaemon`` interface. Inbound flow:

  wuzapi POST → :class:`build_webhook_app` → :meth:`WuzapiAdapter.handle_event`
                                            → :class:`PlatformMessage`

The caller (REPL startup wiring) is responsible for hosting the webhook
server on a chosen loopback port and forwarding parsed envelopes to
:meth:`handle_event`. That separation keeps the adapter free of HTTP
server lifecycle concerns.

Outbound flow uses the typed :class:`WuzapiClient` directly.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any, Final, cast

from obscura.integrations.messaging.identity import normalize_identity
from obscura.integrations.messaging.models import PlatformMessage
from obscura.integrations.whatsapp.wuzapi.client import WuzapiClient, WuzapiError
from obscura.integrations.whatsapp.wuzapi.models import (
    WuzapiSendTextRequest,
    WuzapiWebhookEnvelope,
)

logger = logging.getLogger(__name__)

_PLATFORM: Final[str] = "whatsapp"
_JID_SUFFIXES: Final[tuple[str, ...]] = (
    "@s.whatsapp.net",
    "@g.us",
    "@lid",
    "@broadcast",
)


# ---------------------------------------------------------------------------
# JID helpers
# ---------------------------------------------------------------------------


def _strip_jid(jid: str) -> str:
    """Convert a wuzapi JID into a normalized identity.

    Only direct-message JIDs (``@s.whatsapp.net``) get the ``+`` phone-number
    prefix; group and LID JIDs are opaque identifiers, not numbers.

    Examples::

        "12316333624:14@s.whatsapp.net"  -> "+12316333624"
        "12316333624@s.whatsapp.net"     -> "+12316333624"
        "120363177012345678@g.us"        -> "120363177012345678"  # group
        "187437204672730@lid"            -> "187437204672730"     # LID
        ""                                -> "unknown"

    The trailing ``:<device>`` suffix on direct-message JIDs identifies the
    sending linked-device; we drop it for identity purposes since we want
    one identity per WhatsApp account.
    """
    if not jid:
        return "unknown"
    bare = jid
    matched_suffix: str | None = None
    for suffix in _JID_SUFFIXES:
        if bare.endswith(suffix):
            bare = bare[: -len(suffix)]
            matched_suffix = suffix
            break
    if ":" in bare:
        bare = bare.split(":", 1)[0]
    if matched_suffix == "@s.whatsapp.net" and bare.isdigit():
        return normalize_identity(f"+{bare}")
    return normalize_identity(bare)


_MEDIA_VARIANTS: tuple[tuple[str, str], ...] = (
    ("imageMessage", "image"),
    ("videoMessage", "video"),
    ("documentMessage", "document"),
    ("audioMessage", "voice note"),
    ("stickerMessage", "sticker"),
    ("locationMessage", "location"),
    ("contactMessage", "contact"),
    ("liveLocationMessage", "live location"),
)
"""Maps the wuzapi Message variant key → human-readable label used in
the synthesized text marker. Order doesn't matter (only one variant
is set per Message), but the labels are surfaced to the agent as
``[label] ...`` so keep them short."""


# Maps webhook variant key → (kind discriminator, default mimetype, marker
# text). The marker text MUST match the synthesized text from
# `_from_media` exactly so the service-side rewriter can find and
# replace it with "[<label> at <path>]".
_DOWNLOADABLE_VARIANTS: tuple[tuple[str, str, str, str], ...] = (
    ("imageMessage", "image", "image/jpeg", "[image]"),
    ("videoMessage", "video", "video/mp4", "[video]"),
    ("documentMessage", "document", "application/octet-stream", "[document]"),
    ("audioMessage", "audio", "audio/ogg", "[voice note]"),
)


def _extract_downloadable_media(message: dict[str, Any]) -> dict[str, Any] | None:
    """Return wuzapi-shaped media metadata for the first downloadable
    variant inside ``message``, or ``None`` if there's nothing to download.

    Covers image, video, document, and audio — all four wuzapi
    ``/chat/download*`` endpoints take the same encrypted-media
    metadata shape, so we use one extraction routine and let the
    service layer dispatch to the right endpoint via the ``kind``
    discriminator.

    The returned dict includes:

    * ``kind`` — ``"image" | "video" | "document" | "audio"``
    * ``marker`` — the synthesized text marker the adapter put in
      ``msg.text``, e.g. ``"[image]"`` or ``"[voice note]"``. The
      service uses this for the path-rewrite text replacement.
    * ``url``, ``direct_path``, ``media_key``, ``mimetype``,
      ``file_enc_sha256``, ``file_sha256``, ``file_length`` — fields
      :class:`WuzapiDownloadMediaRequest` expects.

    Searches inside ephemeral / view-once wrappers too — the
    download-fields live on the nested media payload there.
    """

    def _from(inner: Any) -> dict[str, Any] | None:
        if not isinstance(inner, dict):
            return None
        inner_d: dict[str, Any] = cast("dict[str, Any]", inner)
        for variant_key, kind, default_mt, marker in _DOWNLOADABLE_VARIANTS:
            media = inner_d.get(variant_key)
            if not isinstance(media, dict):
                continue
            media_d: dict[str, Any] = cast("dict[str, Any]", media)
            url = media_d.get("url")
            if not isinstance(url, str) or not url:
                return None
            # File length arrives as a string in some webhook payloads,
            # int in others. Tolerate both.
            file_length_raw = media_d.get("fileLength", 0)
            try:
                file_length = int(file_length_raw) if file_length_raw else 0
            except (TypeError, ValueError):
                file_length = 0
            return {
                "kind": kind,
                "marker": marker,
                "url": url,
                "direct_path": str(media_d.get("directPath") or ""),
                "media_key": str(media_d.get("mediaKey") or ""),
                "mimetype": str(media_d.get("mimetype") or default_mt),
                "file_enc_sha256": str(media_d.get("fileEncSha256") or ""),
                "file_sha256": str(media_d.get("fileSha256") or ""),
                "file_length": file_length,
            }
        return None

    direct = _from(message)
    if direct:
        return direct
    for wrapper in ("ephemeralMessage", "viewOnceMessage", "viewOnceMessageV2"):
        outer = message.get(wrapper)
        if isinstance(outer, dict):
            outer_d: dict[str, Any] = cast("dict[str, Any]", outer)
            payload = _from(outer_d.get("message"))
            if payload is not None:
                return payload
    return None


def _from_media(inner_d: dict[str, Any]) -> str:
    """If the message is a media variant, synthesize a text marker.

    Output forms (only the relevant fields are included):

    * ``[image]`` — image with no caption
    * ``[image] caption: please look`` — image with caption
    * ``[document] (invoice.pdf)`` — document with filename, no caption
    * ``[document] (invoice.pdf) caption: my receipt`` — document with both
    * ``[location] name: Times Square address: 7th Ave``

    Returns ``""`` if the message isn't a recognized media variant —
    caller falls through to the next probe.
    """
    for media_key, label in _MEDIA_VARIANTS:
        media = inner_d.get(media_key)
        if not isinstance(media, dict):
            continue
        media_d: dict[str, Any] = cast("dict[str, Any]", media)
        parts: list[str] = [f"[{label}]"]
        filename = media_d.get("fileName")
        if isinstance(filename, str) and filename.strip():
            parts.append(f"({filename.strip()})")
        caption = media_d.get("caption")
        if isinstance(caption, str) and caption.strip():
            parts.append(f"caption: {caption.strip()}")
        name = media_d.get("name")
        if isinstance(name, str) and name.strip():
            parts.append(f"name: {name.strip()}")
        address = media_d.get("address")
        if isinstance(address, str) and address.strip():
            parts.append(f"address: {address.strip()}")
        return " ".join(parts)
    return ""


def _extract_text(message: dict[str, Any]) -> str:
    """Pull text from the wuzapi Message dict.

    wuzapi forwards whatsmeow's union shape verbatim, so the text can live
    in any of several keys depending on the message variant:

    * ``conversation``                                 — plain text
    * ``extendedTextMessage.text``                     — replies, forwards
    * ``ephemeralMessage.message.conversation``        — disappearing msgs
    * ``ephemeralMessage.message.extendedTextMessage.text``
    * ``viewOnceMessage.message.{conversation|extendedTextMessage.text}``

    Media variants (image, video, document, voice note, sticker,
    location, contact) don't have raw text — they have media payloads
    plus optional caption/filename/name fields. We synthesize a marker
    like ``[image]`` or ``[image] caption: please look`` so the agent
    receives *something* and can acknowledge the media, rather than the
    event getting dropped as blank-text. Future: vision-capable
    backends could receive the actual media payload via wuzapi's
    download endpoints.

    We probe these in order. If none match, we log + return an empty
    string so the caller can decide whether to drop the event.
    """

    def _from_inner(inner: Any) -> str:
        if not isinstance(inner, dict):
            return ""
        inner_d: dict[str, Any] = cast("dict[str, Any]", inner)
        conv = inner_d.get("conversation")
        if isinstance(conv, str):
            return conv
        ext = inner_d.get("extendedTextMessage")
        if isinstance(ext, dict):
            ext_d: dict[str, Any] = cast("dict[str, Any]", ext)
            text = ext_d.get("text")
            if isinstance(text, str):
                return text
        media_marker = _from_media(inner_d)
        if media_marker:
            return media_marker
        return ""

    direct = _from_inner(message)
    if direct:
        return direct
    for wrapper in ("ephemeralMessage", "viewOnceMessage", "viewOnceMessageV2"):
        outer = message.get(wrapper)
        if isinstance(outer, dict):
            outer_d: dict[str, Any] = cast("dict[str, Any]", outer)
            text = _from_inner(outer_d.get("message"))
            if text:
                return text
    logger.debug(
        "wuzapi: no text extractable from message variant: %s", sorted(message.keys())
    )
    return ""


def _parse_ts(info: dict[str, Any]) -> datetime:
    """Parse the wuzapi Info.Timestamp ISO8601 string into a UTC datetime."""
    raw = info.get("Timestamp")
    if not isinstance(raw, str):
        return datetime.now(tz=UTC)
    try:
        # whatsmeow emits e.g. "2026-05-11T18:07:45-04:00"
        return datetime.fromisoformat(raw).astimezone(UTC)
    except ValueError:
        logger.warning("wuzapi: unparseable timestamp %r, defaulting to now", raw)
        return datetime.now(tz=UTC)


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class WuzapiAdapter:
    """Adapter from wuzapi events to obscura's :class:`PlatformMessage`.

    Construction is cheap — pass a configured :class:`WuzapiClient`. Call
    :meth:`start` once to verify the WhatsApp session is linked before
    treating the adapter as ready.

    :param client: An owned :class:`WuzapiClient` for HTTP traffic. The
        adapter does not manage the client's lifecycle — caller closes it.
    :param account_id: Stable label for this WhatsApp account inside
        obscura's conversation state store. Default ``"default"``.
    :param drop_from_me: If ``True`` (default), inbound events where
        ``IsFromMe == True`` are ignored. This prevents echoes from our
        own linked-device sends from looping back into the agent.
    :param drop_non_text: If ``True`` (default), events whose extracted
        text is empty are ignored. Set to ``False`` if you want
        placeholders for media messages.
    """

    def __init__(
        self,
        client: WuzapiClient,
        *,
        account_id: str = "default",
        drop_non_text: bool = True,
        echo_window_s: float = 120.0,
    ) -> None:
        self._client = client
        self._account_id = account_id
        self._drop_non_text = drop_non_text
        # Echo detection: messages we sent via this adapter's `send()` come
        # back as inbound Message events (IsFromMe=true) from whatsmeow's
        # event loop. Track recent outbound IDs and drop matches in
        # handle_event. Window must outlive any reasonable network round-
        # trip (default 2 minutes is generous).
        self._echo_window_s = echo_window_s
        self._recent_sends: dict[str, float] = {}
        # Text-based echo backstop. Wuzapi's send() returns one ID format
        # (3EB0...) but whatsmeow's looped-back IsFromMe=true events use a
        # different ID family (3A...). Pure ID matching misses every echo.
        # Comparing text + IsFromMe gate catches the real-world loop.
        self._recent_send_texts: list[tuple[str, float]] = []

    # ---------- lifecycle ----------

    async def start(self) -> None:
        """Verify the WhatsApp session is linked before declaring ready.

        Raises :class:`RuntimeError` if the session is connected to wuzapi
        but no WhatsApp account is actually linked (loggedIn=false).
        """
        status = await self._client.session_status()
        if not status.logged_in:
            raise RuntimeError(
                f"wuzapi session not linked (connected={status.connected}). "
                "Run `obscura whatsapp link` to scan QR and link your account."
            )
        logger.info(
            "wuzapi adapter ready: jid=%s account_id=%s", status.jid, self._account_id
        )

    # ---------- inbound (polling protocol, kept for compat) ----------

    async def poll(self) -> list[PlatformMessage]:
        """Always returns ``[]`` — wuzapi is webhook-driven, not pollable.

        Kept for symmetry with the existing ``MessagePlatformAdapter``
        protocol so ``ChannelDaemon`` and friends can treat both transports
        uniformly. Inbound goes through :meth:`handle_event` instead.
        """
        return []

    # ---------- outbound ----------

    async def send(self, recipient: str, text: str) -> bool:
        """Send a text message to ``recipient``.

        Recipient forms accepted (in order of specificity):

        * ``"<anything>@<server>"`` — full JID (group, DM, LID, etc.).
          Passed through verbatim; wuzapi's parseJID handles routing.
        * ``"group:<group_jid_digits>"`` — channel_id form for group threads.
          Reconstructs ``<digits>@g.us``.
        * Phone number (digits, optionally ``+``-prefixed) — digit-stripped
          and sent as a direct message via wuzapi's default user server.
        """
        if "@" in recipient:
            wire_target = recipient  # full JID — let wuzapi parseJID route it
        elif recipient.startswith("group:"):
            wire_target = f"{recipient.removeprefix('group:')}@g.us"
        else:
            wire_target = re.sub(r"\D", "", recipient)
            if not wire_target:
                logger.warning(
                    "wuzapi.send: empty recipient after digit-strip: %r", recipient
                )
                return False
        try:
            resp = await self._client.send_text(
                WuzapiSendTextRequest(phone=wire_target, body=text)
            )
        except WuzapiError:
            logger.exception("wuzapi.send to %s failed", wire_target)
            return False
        # Record both the wuzapi-returned ID AND the message text so
        # handle_event can drop echoes by either fingerprint. Pure ID
        # matching fails because wuzapi's send-returned IDs differ from
        # whatsmeow's inbound-event IDs for the same wire-level message.
        now = time.time()
        self._recent_sends[resp.id] = now
        self._recent_send_texts.append((text, now))
        if len(self._recent_sends) > 256:
            cutoff = now - self._echo_window_s * 2
            self._recent_sends = {
                k: v for k, v in self._recent_sends.items() if v >= cutoff
            }
        if len(self._recent_send_texts) > 256:
            self._recent_send_texts = [
                (t, ts)
                for (t, ts) in self._recent_send_texts
                if now - ts < self._echo_window_s * 2
            ]
        logger.info("wuzapi.send id=%s to=%s", resp.id, wire_target)
        return True

    # ---------- webhook event handling ----------

    def handle_event(self, envelope: WuzapiWebhookEnvelope) -> PlatformMessage | None:
        """Convert a wuzapi webhook envelope to a :class:`PlatformMessage`.

        Returns ``None`` if the event isn't a normal inbound text we should
        forward (own message, non-text payload, unknown event type, etc.).
        Never raises — invalid shapes are logged and dropped.
        """
        if envelope.type != "Message":
            logger.debug("wuzapi: skipping non-Message event %r", envelope.type)
            return None

        info_raw = envelope.event.get("Info")
        msg_raw = envelope.event.get("Message")
        if not isinstance(info_raw, dict) or not isinstance(msg_raw, dict):
            logger.warning("wuzapi: malformed Message envelope (missing Info/Message)")
            return None
        # Cast narrows from `dict[Unknown, Unknown]` (post-isinstance) to the
        # opaque-payload type the helpers expect. We've already validated the
        # outer shape; per-key access is best-effort downstream.
        info: dict[str, Any] = cast("dict[str, Any]", info_raw)
        msg: dict[str, Any] = cast("dict[str, Any]", msg_raw)

        # Extract text first so echo detection + blank filter can use it.
        text = _extract_text(msg)

        # Echo detection — two-layer:
        #   1. ID match: drops events whose ID was returned by our recent
        #      send() call. Cheap, exact, but fails when wuzapi's
        #      send-returned ID family (3EB0...) differs from whatsmeow's
        #      inbound-event ID family (3A...) for the same wire message.
        #   2. Text match GATED on IsFromMe=true: drops IsFromMe events
        #      whose text exactly matches a recent send. The IsFromMe gate
        #      prevents false positives where a contact quotes the bot's
        #      reply back to us (the quote isn't IsFromMe).
        msg_id_check = str(info.get("ID") or "")
        now = time.time()
        if msg_id_check and msg_id_check in self._recent_sends:
            if now - self._recent_sends[msg_id_check] < self._echo_window_s:
                logger.debug("wuzapi: dropping ID-matched echo id=%s", msg_id_check)
                return None
        if bool(info.get("IsFromMe")):
            text_stripped = text.strip()
            for sent_text, sent_at in self._recent_send_texts:
                if now - sent_at > self._echo_window_s:
                    continue
                if text_stripped == sent_text.strip():
                    logger.debug(
                        "wuzapi: dropping text-matched echo (IsFromMe=true) len=%d",
                        len(text_stripped),
                    )
                    return None

        if self._drop_non_text and not text.strip():
            # Catches: empty string, whitespace-only, and message variants
            # (delivery receipts, system events, etc.) whose extractable
            # text is blank. Don't waste an agent turn on these.
            logger.debug("wuzapi: skipping blank-text event")
            return None

        sender_jid = str(info.get("Sender") or "")
        chat_jid = str(info.get("Chat") or "")
        is_group = bool(info.get("IsGroup"))
        message_id = str(info.get("ID") or "")

        sender_id = _strip_jid(sender_jid)
        if is_group:
            channel_id = f"group:{_strip_jid(chat_jid)}"
        else:
            channel_id = f"dm:{sender_id}"
        # ``recipient_id`` is the linked account's perspective — always
        # ``"me"`` for inbound, regardless of DM vs group. Matches the
        # convention used by every other adapter in the codebase
        # (Twilio WhatsApp, iMessage, Slack, Discord). The DM-vs-group
        # distinction lives in ``channel_id`` and ``metadata["is_group"]``.
        recipient_id = "me"

        if not message_id:
            # Synthesize a stable ID for dedup
            message_id = hashlib.sha1(
                f"{sender_jid}|{info.get('Timestamp', '')}|{text}".encode()
            ).hexdigest()

        metadata: dict[str, Any] = {
            "wuzapi_message_id": message_id,
            "jid_sender": sender_jid,
            "jid_chat": chat_jid,
            "is_group": is_group,
            "is_from_me": bool(info.get("IsFromMe")),
            "message_type": info.get("Type"),
            "push_name": info.get("PushName"),
        }
        # If this is a downloadable media message, surface the wuzapi-
        # shaped download metadata so the service layer can pull the
        # bytes and save them. Stays absent for text-only or non-
        # downloadable variants (sticker/location/contact).
        media_payload = _extract_downloadable_media(msg)
        if media_payload is not None:
            metadata["media_payload"] = media_payload
        # **Fast path**: wuzapi's processMedia hook runs server-side and
        # embeds the decoded media bytes as a top-level `base64` field
        # in the webhook envelope (mode is "base64" or "both" — the
        # default). Surface this directly so the service can decode +
        # save without a round-trip to /chat/download*. Critical
        # because WhatsApp's CDN URLs in the inner imageMessage are
        # single-use — they've already been consumed by processMedia,
        # so /chat/download* would fail re-downloading them.
        if envelope.base64:
            metadata["inline_media_b64"] = envelope.base64
            metadata["inline_media_mimetype"] = envelope.mime_type or ""
            metadata["inline_media_filename"] = envelope.file_name or ""

        return PlatformMessage(
            platform=_PLATFORM,
            account_id=self._account_id,
            channel_id=channel_id,
            sender_id=sender_id,
            recipient_id=recipient_id,
            message_id=message_id,
            text=text,
            timestamp=_parse_ts(info),
            metadata=metadata,
        )

    def handle_events(
        self, envelopes: Iterable[WuzapiWebhookEnvelope]
    ) -> list[PlatformMessage]:
        """Batch convenience over :meth:`handle_event`."""
        return [m for e in envelopes if (m := self.handle_event(e)) is not None]


__all__ = ["WuzapiAdapter"]
